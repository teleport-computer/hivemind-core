# Async Query Pipeline

`POST /v1/query-agents/submit` 的完整三阶段流程。

---

## 请求

```
POST /v1/query-agents/submit  (multipart/form-data)
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `archive` | file | 是 | 包含 Dockerfile 的 tar.gz 压缩包 |
| `name` | string | 是 | Agent 名称 |
| `prompt` | string | 否 | 查询提示词 |
| `scope_agent_id` | string | 否 | 指定 scope agent（默认使用系统配置） |
| `mediator_agent_id` | string | 否 | 指定 mediator agent（默认使用系统配置） |
| `max_llm_calls` | int | 否 | 最大 LLM 调用次数（默认 20） |
| `max_tokens` | int | 否 | 总 token 预算（默认 100,000） |
| `timeout_seconds` | int | 否 | 执行超时（默认 120s） |

**响应**（立即返回）：

```json
{
  "run_id": "5f3576f4100c",
  "agent_id": "f1afc7118743",
  "status": "pending"
}
```

---

## 异步执行流程

```
用户
  │
  │  POST /v1/query-agents/submit
  │  (archive, prompt, scope_agent_id?, mediator_agent_id?)
  │
  ▼
┌─────────────────────────────────────────────────────────────┐
│  hivemind-core                                              │
│                                                             │
│  1. 解压 tar.gz → docker build → 注册 agent                │
│  2. 创建 run record (status: pending)                       │
│  3. 立即返回 { run_id, agent_id, status: "pending" }        │
│                                                             │
│  ═══════════════ 以下为异步后台执行 ═══════════════          │
│                                                             │
│  ┌───────────────────────────────────────────────────┐      │
│  │  Stage 0: Scope Agent                             │      │
│  │                                                   │      │
│  │  使用: scope_agent_id 或 default_scope_agent      │      │
│  │  权限: FULL_READ (可读所有用户表)                   │      │
│  │  能力: 查 DB schema、读 query agent 源码、模拟执行  │      │
│  │  输出: scope_fn(sql, params, rows) → 过滤函数      │      │
│  │                                                   │      │
│  │  失败时: warning 日志，继续执行(无 scope 过滤)      │      │
│  └─────────────────────┬─────────────────────────────┘      │
│                        │ scope_fn                            │
│                        ▼                                     │
│  ┌───────────────────────────────────────────────────┐      │
│  │  Stage 1: Query Agent (用户上传的自定义 agent)      │      │
│  │                                                   │      │
│  │  权限: SCOPED (SQL 结果经 scope_fn 过滤)           │      │
│  │  能力: execute_sql, get_schema, llm/chat,          │      │
│  │       artifact-upload                             │      │
│  │                                                   │      │
│  │  execute_sql 流程:                                 │      │
│  │    agent → bridge → DB proxy → 拿到原始 rows       │      │
│  │                   → scope_fn(sql, params, rows)    │      │
│  │                   → 返回过滤后的 rows 给 agent      │      │
│  │                                                   │      │
│  │  artifact-upload 流程:                             │      │
│  │    agent → bridge → ArtifactStore (Postgres BYTEA)│      │
│  │                   → 返回 /v1/query/runs/.../path   │      │
│  │                   → 24h TTL 后自动清理              │      │
│  │                                                   │      │
│  │  输出: stdout (query 结果文本)                      │      │
│  └─────────────────────┬─────────────────────────────┘      │
│                        │ query_output                        │
│                        ▼                                     │
│  ┌───────────────────────────────────────────────────┐      │
│  │  Stage 2: Mediator Agent                          │      │
│  │                                                   │      │
│  │  使用: mediator_agent_id 或 default_mediator_agent│      │
│  │  权限: NONE (无 DB/工具访问)                       │      │
│  │  输入: RAW_OUTPUT = query agent 的原始输出          │      │
│  │  能力: 仅 LLM 调用，审计/脱敏                      │      │
│  │  输出: 过滤后的安全文本                             │      │
│  │                                                   │      │
│  │  跳过条件: token 预算不足 (<128)                    │      │
│  │  失败时: warning 日志，返回未审计的原始输出          │      │
│  └─────────────────────┬─────────────────────────────┘      │
│                        │                                     │
│                        ▼                                     │
│  更新 run record → status: completed                         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
  │
  │  GET /v1/query-agents/runs/{run_id}  (轮询)
  │
  ▼
{
  "run_id": "5f3576f4100c",
  "status": "completed",
  "artifacts": [
    {
      "filename": "report.json",
      "content_type": "application/json",
      "size_bytes": 2431,
      "created_at": 1711612379.123
    }
  ],
  "artifact_retention_seconds": 86400,
  "error": null
}
```

---

## Token 预算分配

```
总预算 max_tokens (如 100,000)
  │
  ├── Stage 0: Scope Agent 消耗 → 剩余 remaining
  │
  ├── Stage 1: Query Agent 消耗 → remaining - mediator 预留(512)
  │
  └── Stage 2: Mediator 消耗 → 用剩余预算，不足 128 则跳过
```

---

## 三阶段隔离对比

| | Scope Agent | Query Agent | Mediator Agent |
|---|---|---|---|
| **DB 权限** | FULL_READ | SCOPED (经 scope_fn 过滤) | 无 |
| **工具** | execute_sql, get_schema, 读 agent 源码, simulate | execute_sql, get_schema, artifact-upload | 无 |
| **网络** | 仅 Bridge | 仅 Bridge | 仅 Bridge |
| **容器** | 独立 Docker，用完即删 | 独立 Docker，用完即删 | 独立 Docker，用完即删 |

---

## 轮询

```
GET /v1/query-agents/runs/{run_id}
```

**状态流转**: `pending` → `running` → `completed` / `failed`

| 字段 | 说明 |
|------|------|
| `run_id` | 任务 ID |
| `agent_id` | Query agent ID |
| `status` | pending / running / completed / failed |
| `artifacts` | agent 写入的 artifact 列表（filename, size_bytes, content_type, created_at） |
| `artifact_retention_seconds` | artifact 保留时间（默认 86400 = 24h） |
| `error` | 失败时的错误信息 |

artifact 下载： `GET /v1/query/runs/{run_id}/artifacts/{filename}` 返回原始字节；`X-Retention-Seconds` header 说明剩余可用时间。

**轮询示例**：

```bash
RUN_ID="5f3576f4100c"

while true; do
  RESULT=$(curl -s "$CORE_URL/v1/query-agents/runs/$RUN_ID")
  STATUS=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "Status: $STATUS"

  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
    echo "$RESULT" | python3 -m json.tool
    break
  fi
  sleep 5
done
```
