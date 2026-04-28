# TikTok Analytics Agent — 端到端使用指南

## 前置条件

- hivemind-core 正在运行 (本地 `localhost:8100` 或线上实例)
- 数据库中已存在 `data_xordi_tiktok_oauth_watch_history` 表 (50 条记录)
- 报告存储由内置的 Postgres artifact store 提供，保留 24 小时 (由 `HIVEMIND_ARTIFACT_RETENTION_SECONDS` 配置)

```bash
# 设置变量 (根据实际环境修改)
export CORE_URL="http://localhost:8100"
# 如果线上实例需要认证:
# export API_KEY="your-api-key"
# export AUTH="-H 'Authorization: Bearer $API_KEY'"
```

---

## 完整流程

### Step 1: 打包 agent

```bash
cd agents/examples/tiktok-analytics
tar czf /tmp/tiktok-analytics.tar.gz -C . .
```

### Step 2: 上传并提交异步任务

通过 `/v1/query-agents/submit` 一步完成上传 + 启动执行：

```bash
curl -s -X POST "$CORE_URL/v1/query-agents/submit" \
  -F "name=tiktok-analytics" \
  -F "archive=@/tmp/tiktok-analytics.tar.gz" \
  -F "prompt=Analyse TikTok watch history" \
  -F "description=Summarise hashtag themes and per-user stats, upload report to artifact store" \
  -F "max_llm_calls=5" \
  -F "max_tokens=50000" \
  -F "timeout_seconds=120" \
  | python3 -m json.tool
```

返回示例：

```json
{
    "run_id": "a1b2c3d4e5f6",
    "agent_id": "f6e5d4c3b2a1",
    "status": "pending"
}
```

记住 `run_id`，后续用来轮询结果。

```bash
export RUN_ID="a1b2c3d4e5f6"  # 替换为实际返回值
```

### Step 3: 轮询任务状态

```bash
curl -s "$CORE_URL/v1/agent-runs/$RUN_ID" | python3 -m json.tool
```

**状态流转：** `pending` → `running` → `completed` / `failed`

运行中的返回：

```json
{
    "run_id": "a1b2c3d4e5f6",
    "agent_id": "f6e5d4c3b2a1",
    "status": "running",
    "error": null,
    "created_at": 1711612345.123,
    "updated_at": 1711612350.456,
    "artifacts": [],
    "artifact_retention_seconds": 86400
}
```

完成后的返回（包含已写入的 artifact 列表，通过服务端接口下载）：

```json
{
    "run_id": "a1b2c3d4e5f6",
    "agent_id": "f6e5d4c3b2a1",
    "status": "completed",
    "error": null,
    "created_at": 1711612345.123,
    "updated_at": 1711612380.789,
    "artifacts": [
        {
            "filename": "report.json",
            "content_type": "application/json",
            "size_bytes": 2431,
            "created_at": 1711612379.123
        }
    ],
    "artifact_retention_seconds": 86400
}
```

下载 artifact：

```bash
curl -s "$CORE_URL/v1/query/runs/$RUN_ID/artifacts/report.json" -o report.json
```

### Step 4: 简单轮询脚本

```bash
while true; do
  STATUS=$(curl -s "$CORE_URL/v1/agent-runs/$RUN_ID")
  echo "$STATUS" | python3 -m json.tool

  S=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  if [ "$S" = "completed" ] || [ "$S" = "failed" ]; then
    break
  fi
  sleep 3
done
```

---

## Agent 内部做了什么

```
┌─────────────────────────────────────────────────────┐
│  tiktok-analytics agent (Docker container)          │
│                                                     │
│  1. POST /tools/execute_sql                         │
│     → SELECT ... FROM watch_history ORDER BY ...    │
│     → 获取 50 条记录                                  │
│                                                     │
│  2. 本地统计                                          │
│     → unique_users, unique_authors                  │
│     → top 20 hashtags by frequency                  │
│                                                     │
│  3. POST /llm/chat                                  │
│     → LLM 分析 hashtag 主题、内容分类、观看模式        │
│     → 返回结构化 JSON                                │
│                                                     │
│  4. POST /sandbox/artifact-upload                   │
│     → 写入 report.json 到 Postgres artifact store    │
│     → 通过 /v1/query/runs/{run_id}/artifacts 下载    │
│     → 24 小时 TTL 自动清理                           │
│                                                     │
│  5. stdout → JSON summary                           │
└─────────────────────────────────────────────────────┘
```

---

## 报告示例

artifact store 中的 `report.json` 结构：

```json
{
  "run_id": "a1b2c3d4e5f6",
  "statistics": {
    "total_videos": 50,
    "unique_users": 4,
    "unique_authors": 45,
    "total_hashtags_used": 120,
    "unique_hashtags": 85,
    "top_20_hashtags": [
      {"tag": "fyp", "count": 8},
      {"tag": "tiktok", "count": 3}
    ]
  },
  "llm_analysis": {
    "themes": ["Entertainment & comedy", "Music nostalgia", "..."],
    "categories": ["Comedy/Humor", "Music", "Lifestyle", "..."],
    "patterns": ["High engagement on comedy content", "..."]
  }
}
```

---

## 备选：仅注册 agent，不立即执行

如果只想上传 agent 以便后续复用：

```bash
# 仅上传注册
curl -s -X POST "$CORE_URL/v1/agents/upload" \
  -F "name=tiktok-analytics" \
  -F "archive=@/tmp/tiktok-analytics.tar.gz" \
  | python3 -m json.tool
# → {"agent_id": "...", "name": "tiktok-analytics", "files_extracted": 3}

# 之后通过 /v1/query/run/submit 指定 agent_id 执行
curl -s -X POST "$CORE_URL/v1/query/run/submit" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Analyse TikTok watch history",
    "query_agent_id": "<agent_id>"
  }' | python3 -m json.tool
# → {"run_id": "...", "status": "running"}
```
