# hivemind-core

一个**中立的加密存储**与 **Docker agent 沙箱**平台。它有点像“面向 AI 介导知识的 Postgres”——应用通过注册 Docker agent 镜像来定义自己的元数据、访问控制与查询逻辑。

核心只提供不可约的基础能力：**加密记录存储**、**FTS5 全文检索**、**Docker 沙箱**、**scope（范围）强约束**、以及**管线编排**。

## 快速开始

```bash
# 安装
uv sync --all-extras

# 配置
cp .env.example .env
# 编辑 .env —— 至少需要设置 HIVEMIND_LLM_API_KEY

# 构建本地默认 agent 镜像（.env.example 配置会用到）
docker build -t hivemind-default-index:local agents/default-index
docker build -t hivemind-default-query:local agents/default-query
docker build -t hivemind-default-scope:local agents/default-scope
docker build -t hivemind-default-mediator:local agents/default-mediator

# 运行
uv run python -m hivemind.server

# 验证
curl http://localhost:8100/v1/health
```

## 工作原理

### 系统总览

```
                       ┌────────────────────────────────┐
                       │        CLIENT / CALLER          │
                       │   (curl, httpx, any HTTP client) │
                       └────────┬──────────┬─────────────┘
                                │          │
                       POST /v1/store   POST /v1/query
                                │          │
                       ┌────────▼──────────▼─────────────┐
                       │    FastAPI Server (server.py)    │
                       │    http://localhost:8100         │
                       │                                  │
                       │    Auth: Bearer HIVEMIND_API_KEY  │
                       └────────┬──────────┬─────────────┘
                                │          │
                       ┌────────▼──────────▼─────────────┐
                       │    Pipeline (pipeline.py)        │
                       │                                  │
                       │  Store: data → index → write     │
                       │  Query: scope → query → mediator │
                       │                                  │
                       │  Tracks token budgets per stage   │
                       └──┬────────┬────────┬────────────┘
                          │        │        │
                 ┌────────▼─┐  ┌───▼───┐  ┌─▼────────────┐
                 │RecordStore│  │Agent  │  │Sandbox       │
                 │           │  │Store  │  │Backend       │
                 │SQLite+FTS5│  │       │  │              │
                 │Fernet enc │  │CRUD + │  │Docker runner │
                 │Scope WHERE│  │files  │  │Bridge server │
                 └───────────┘  └───────┘  └──────────────┘
```

### 写入管线（`POST /v1/store`）

```
客户端发送：
  { data: "Sprint retro notes...",
    metadata: {"author": "alice"},
    index_agent_id: "idx-1"  }       ← 或者 index_text: "预先计算"
            │
            ▼
优先级：index_text > index_agent_id > 默认 index agent > 不做索引
            │
    ┌───────▼────────────────────────────────┐
    │ Index Agent 容器（Docker）              │
    │                                        │
    │ ENV（建议值）：                         │
    │   DOCUMENT_DATA = "Sprint retro…"      │
    │   DOCUMENT_METADATA = {"author":…}     │
    │                                        │
    │ TOOLS：search, read, list              │
    │                                        │
    │ stdout → JSON：                         │
    │   {"index_text": "...",                │
    │    "metadata": {"tags": [...]}}        │
    └───────┬────────────────────────────────┘
            │
            ▼
  RecordStore.write_record()
    - 使用 Fernet 加密 data
    - 存储 metadata JSON
    - 如果提供 index_text，则写入 FTS 索引
            │
            ▼
  响应：{ record_id, created_at, metadata }
```

### 查询管线（`POST /v1/query`）

```
客户端发送：
  { query: "What decisions were made?",
    query_agent_id: "qa-1",
    scope_agent_id: "scope-1",          ← 可选
    mediator_agent_id: "med-1",         ← 可选
    max_tokens: 100000 }                ← 可选：预算上限
            │
            ▼
═══ 阶段 0：SCOPE（可选） ═══════════════════════════════

  ┌──────────────────────────────────────────────────────┐
  │ Scope Agent 容器                                      │
  │                                                      │
  │ ENV：QUERY_PROMPT, QUERY_AGENT_ID                    │
  │ TOOLS：search, read, list（全量访问，无 scope 限制）  │
  │        list_query_agent_files, read_query_agent_file  │
  │ BRIDGE 额外能力：                                     │
  │   POST /sandbox/simulate  ← 运行嵌套 query            │
  │   GET  /sandbox/agents/{id}/files                      │
  │                                                      │
  │ stdout → {"record_ids": ["r1", "r2", "r3"]}         │
  └─────────────────────────┬────────────────────────────┘
                            │
                  scope = ["r1","r2","r3"]
                  remaining_tokens -= scope_usage
                            │
                            ▼
═══ 阶段 1：QUERY ══════════════════════════════════════════

  ┌──────────────────────────────────────────────────────┐
  │ Query Agent 容器                                     │
  │                                                      │
  │ ENV：QUERY_PROMPT                                    │
  │ TOOLS：search, read, list（限定在 r1,r2,r3）          │
  │                                                      │
  │   search("migration") → 只会命中 r1,r2,r3             │
  │   read("r4")          → "Record not found"（被 scope 拦截） │
  │   list()              → 只会列出 r1,r2,r3              │
  │                                                      │
  │ stdout → "The team decided to migrate to Stripe…"     │
  └─────────────────────────┬────────────────────────────┘
                            │
                  output + records_accessed = ["r1","r3"]
                  remaining_tokens -= query_usage
                            │
                            ▼
═══ 阶段 2：MEDIATOR（可选） ════════════════════════════

  ┌──────────────────────────────────────────────────────┐
  │ Mediator Agent 容器                                  │
  │                                                      │
  │ ENV：RAW_OUTPUT, QUERY_PROMPT, RECORDS_ACCESSED       │
  │ TOOLS：无（mediator 没有任何数据访问能力）            │
  │                                                      │
  │ stdout → "[filtered] The team decided to migrate…"    │
  └─────────────────────────┬────────────────────────────┘
                            │
                            ▼
  响应：
    { output: "[filtered] The team decided…",
      records_accessed: ["r1", "r3"],
      mediated: true,
      usage: { total_tokens: 8500, max_tokens: 100000 } }
```

### 每个 agent 容器都会收到的内容

```
┌──────────── 强制注入（所有 agents 都会收到，无法绕过） ─────────┐
│                                                               │
│  BRIDGE_URL         http://host.docker.internal:<port>        │
│  SESSION_TOKEN      随机的 32 字节 urlsafe token               │
│  AGENT_ROLE         query | scope | index | mediator          │
│  BUDGET_MAX_TOKENS  本次运行剩余 token 预算                    │
│  BUDGET_MAX_CALLS   本次运行剩余调用次数预算                   │
│  OPENAI_BASE_URL    http://host.docker.internal:<port>/v1     │
│  OPENAI_API_KEY     与 SESSION_TOKEN 相同                     │
│                                                               │
│  Bridge 是唯一网络出口。OpenAI SDK 可在无需改代码的情况下       │
│  自动通过 bridge 转发。                                       │
└───────────────────────────────────────────────────────────────┘

┌──────────── 建议值（按角色不同，可忽略） ───────────────────────┐
│                                                               │
│  Index：    DOCUMENT_DATA, DOCUMENT_METADATA                  │
│  Scope：    QUERY_PROMPT, QUERY_AGENT_ID                      │
│  Query：    QUERY_PROMPT                                      │
│  Mediator： RAW_OUTPUT, QUERY_PROMPT, RECORDS_ACCESSED        │
│                                                               │
│  默认 agents 会用到这些变量。自定义 agent 可以完全忽略它们      │
│  —— agent 本质上只是一个 Docker 容器，可自行决定行为。          │
└───────────────────────────────────────────────────────────────┘
```

### 容器内部：bridge 作为唯一出口

```
┌───────────────────────────────────────────────────────────────┐
│                 Docker 内部网络                                 │
│               (hivemind-sandbox, internal=true)                 │
│                                                               │
│  ┌─────────────────────┐        ┌──────────────────────────┐  │
│  │  Agent 容器         │        │  Bridge Server           │  │
│  │                     │        │  （每次运行临时创建）     │  │
│  │  只读 rootfs        │  HTTP  │                          │  │
│  │  drop ALL caps      │◄─────►│  GET  /health            │  │
│  │  no-new-privileges  │  only  │  GET  /tools             │  │
│  │  256MB 内存上限     │  exit  │  POST /tools/{name}      │  │
│  │  1 CPU, 256 PIDs    │        │  POST /llm/chat          │  │
│  │                     │        │  POST /v1/chat/completions│  │
│  │  ┌───────────────┐  │        │       （OpenAI 兼容）     │  │
│  │  │ Agent 代码    │  │        │                          │  │
│  │  │（任意语言/SDK）│  │        │  Auth：Bearer token      │  │
│  │  └───────────────┘  │        │  Budget：超限返回 429     │  │
│  │                     │        │                          │  │
│  │  stdout = 输出      │        │  scope 专属：             │  │
│  └─────────────────────┘        │  POST /sandbox/simulate  │  │
│                                 │  GET  /sandbox/agents/…  │  │
│                                 └────────────┬─────────────┘  │
│       ✗ 无互联网                               │                │
│       ✗ 无法访问其他容器                        │                │
│       ✗ Linux：每容器 iptables 规则             │                │
└────────────────────────────────────────────────┼────────────────┘
                                               │
                                  ┌────────────▼──────────────┐
                                  │  LLM 提供方               │
                                  │  (OpenRouter, OpenAI,     │
                                  │   Anthropic, etc.)        │
                                  │                           │
                                  │  只有 bridge 连接外部世界  │
                                  └───────────────────────────┘
```

### Scope（范围）强约束

```
RecordStore 里有记录：r1, r2, r3, r4, r5

scope = ["r1", "r2", "r3"]（来自 scope agent 或直接由请求指定）
       │
       ▼
build_tools(store, scope=["r1","r2","r3"])
       │  创建 tool handlers，并把 scope 烘焙到闭包中
       ▼
search("migration")
  → SQL：… WHERE records_fts MATCH 'migration'
         AND r.id IN ('r1','r2','r3')      ← 强制约束
  → 结果中只可能出现 r1, r2, r3

read("r4")
  → SQL：… WHERE r.id = 'r4'
         AND r.id IN ('r1','r2','r3')      ← r4 被阻断
  → "Record not found"

agent 无法绕过这个边界。scope 在管线构建阶段就被烘焙进 Python 闭包，
bridge 也没有任何接口允许它修改 scope。SQL 的 WHERE 子句就是边界。
```

### 预算在各阶段之间的流转

```
max_tokens = 100,000（来自请求或全局上限）
       │
       ▼
┌─ 阶段 0：Scope Agent ─────────────────────────┐
│  预算：100,000 tokens                          │
│  已用：2,000 tokens → 剩余 = 98,000            │
└────────────────────────────────────────────────┘
       │  （若配置 mediator，会预留 512 tokens）
       ▼
┌─ 阶段 1：Query Agent ──────────────────────────┐
│  预算：97,488 tokens                            │
│  已用：45,000 tokens → 剩余 = 53,000            │
└─────────────────────────────────────────────────┘
       │
       ▼
┌─ 阶段 2：Mediator Agent ───────────────────────┐
│  预算：53,000 tokens                            │
│  已用：3,000 tokens                             │
│  （若剩余 < 128 tokens 会跳过 mediator）         │
└─────────────────────────────────────────────────┘
       │
       ▼
响应：usage = { total_tokens: 50,000, max_tokens: 100,000 }

在每个阶段内部，bridge 会对每次 LLM 调用做强制约束：
  agent 调用 /llm/chat 或 /v1/chat/completions
    → bridge 先做 budget.check()（预估 preflight）
    → 若超限 → 429 "Budget exhausted"
    → 若未超限 → 转发到 LLM 提供方
    → 从提供方响应里记录实际 usage
    → 返回给 agent
```

### 安全层

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1：SCOPE（SQL 级别，无法绕过）                   │
│  ───────────────────────────────────────                │
│  WHERE r.id IN (scope_list)                             │
│  工具在物理层面无法访问 scope 外的记录。scope 在管线层面  │
│  被烘焙进工具闭包中。                                    │
├─────────────────────────────────────────────────────────┤
│  Layer 2：DOCKER 隔离（运行时级别）                      │
│  ───────────────────────────────────────                │
│  • 只读根文件系统（/tmp 使用 tmpfs）                     │
│  • 丢弃所有 Linux capabilities                           │
│  • no-new-privileges 安全选项                            │
│  • Docker 内部网络（bridge 是唯一出口）                  │
│  • 内存上限（256MB）、CPU 配额（1 core）                 │
│  • PID 上限（256）                                       │
│  • Linux：在 DOCKER-USER 安装每容器 iptables 规则         │
│    只允许 bridge IP:port，其他全部 DROP                  │
├─────────────────────────────────────────────────────────┤
│  Layer 3：预算强制（bridge 级别）                        │
│  ───────────────────────────────────────                │
│  • max_calls 与 max_tokens 硬上限                         │
│  • 每次 LLM 调用前做预检查                                │
│  • 用尽预算返回 429                                       │
│  • 通过 asyncio Lock 串行化（避免竞态）                   │
├─────────────────────────────────────────────────────────┤
│  Layer 4：静态加密（at-rest）                             │
│  ───────────────────────────────────────                │
│  • records.data 用 Fernet 加密                            │
│  • 没有 HIVEMIND_ENCRYPTION_KEY 数据库文件不可用           │
├─────────────────────────────────────────────────────────┤
│  Layer 5：MEDIATOR（软约束，基于 LLM）                   │
│  ───────────────────────────────────────                │
│  • 可选 agent 审计/过滤 query 输出                        │
│  • 没有任何工具访问（无法外带数据）                       │
│  • 纵深防御的一层 —— 依赖 LLM，但不是硬边界               │
└─────────────────────────────────────────────────────────┘
```

## 配置

所有配置从 `.env` 中以 `HIVEMIND_` 前缀加载。

| 变量 | 默认值 | 说明 |
|----------|---------|-------------|
| `HIVEMIND_DB_PATH` | `./hivemind.db` | SQLite 数据库路径 |
| `HIVEMIND_ENCRYPTION_KEY` | — | 静态加密 Fernet key（留空=明文） |
| `HIVEMIND_API_KEY` | — | HTTP 鉴权共享密钥；绑定到非本地主机时必填 |
| `HIVEMIND_HOST` | `127.0.0.1` | 服务监听地址 |
| `HIVEMIND_PORT` | `8100` | 服务监听端口 |
| `HIVEMIND_CORS_ALLOW_ORIGINS` | — | 浏览器 CORS origins（逗号分隔）；空=不加 CORS 头 |
| `HIVEMIND_LLM_API_KEY` | — | LLM 提供方 API key（bridge 透传给 agents） |
| `HIVEMIND_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | LLM API base URL |
| `HIVEMIND_LLM_MODEL` | `anthropic/claude-sonnet-4.5` | 默认 LLM 模型 |
| `HIVEMIND_LLM_TIMEOUT_SECONDS` | `45` | bridge 对外部 LLM 请求的超时（秒） |
| `HIVEMIND_BRIDGE_HOST` | `0.0.0.0` | bridge 监听地址（必须能被 Docker 容器访问） |
| `HIVEMIND_DOCKER_HOST` | — | 可选：Docker daemon host/socket（例如 `unix:///Users/me/.docker/run/docker.sock`） |
| `HIVEMIND_DOCKER_NETWORK` | `hivemind-sandbox` | 沙箱容器使用的 Docker 网络名 |
| `HIVEMIND_DOCKER_NETWORK_INTERNAL` | `true` | 兼容 host bridge 时启用 Docker internal 网络模式 |
| `HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS` | `true` | 仅 Linux：为每个容器安装 `DOCKER-USER` 防火墙规则，只允许访问 bridge IP:port（macOS/Windows 会忽略） |
| `HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS_FAIL_CLOSED` | `true` | 仅 Linux：如果防火墙配置失败则终止本次 agent 运行（而不是继续） |
| `HIVEMIND_CONTAINER_MEMORY_MB` | `256` | 容器内存上限（MB） |
| `HIVEMIND_CONTAINER_CPU_QUOTA` | `1.0` | 容器 CPU 配额（1.0 = 1 核） |
| `HIVEMIND_CONTAINER_PIDS_LIMIT` | `256` | 每个沙箱容器的最大进程数 |
| `HIVEMIND_CONTAINER_READ_ONLY_FS` | `true` | 以只读 rootfs 运行容器 |
| `HIVEMIND_CONTAINER_DROP_ALL_CAPS` | `true` | 丢弃容器内所有 Linux capabilities |
| `HIVEMIND_CONTAINER_NO_NEW_PRIVILEGES` | `true` | 启用 Docker 的 `no-new-privileges` 安全选项 |
| `HIVEMIND_MAX_LLM_CALLS` | `50` | 每次 agent 运行的全局 LLM 调用次数上限 |
| `HIVEMIND_MAX_TOKENS` | `200000` | 每次 agent 运行的全局 token 上限 |
| `HIVEMIND_AGENT_TIMEOUT` | `300` | agent 最大运行时长（秒） |
| `HIVEMIND_AUTOLOAD_DEFAULT_AGENTS` | `true` | 启动时按稳定 ID 自动注册默认 agents |
| `HIVEMIND_DEFAULT_INDEX_AGENT` | `default-index`（示例） | 默认 index agent ID（空=调用方必须提供 index_text） |
| `HIVEMIND_DEFAULT_QUERY_AGENT` | `default-query`（示例） | 默认 query agent ID（空=请求必须提供 query_agent_id） |
| `HIVEMIND_DEFAULT_SCOPE_AGENT` | `default-scope`（示例） | 默认 scope agent ID（空=不做 scoping） |
| `HIVEMIND_DEFAULT_MEDIATOR_AGENT` | — | 默认 mediator agent ID（空=不做 mediation） |
| `HIVEMIND_DEFAULT_INDEX_IMAGE` | — | 自动载入到 `HIVEMIND_DEFAULT_INDEX_AGENT` 的 Docker image |
| `HIVEMIND_DEFAULT_QUERY_IMAGE` | — | 自动载入到 `HIVEMIND_DEFAULT_QUERY_AGENT` 的 Docker image |
| `HIVEMIND_DEFAULT_SCOPE_IMAGE` | — | 自动载入到 `HIVEMIND_DEFAULT_SCOPE_AGENT` 的 Docker image |
| `HIVEMIND_DEFAULT_MEDIATOR_IMAGE` | — | 自动载入到 `HIVEMIND_DEFAULT_MEDIATOR_AGENT` 的 Docker image |

如果 `HIVEMIND_HOST` 是非本地地址（不是 `127.0.0.1`/`localhost`），启动时若未设置 `HIVEMIND_API_KEY` 会失败。

仓库提供的 `.env.example` 给出一个可直接运行的本地 profile：使用 `default-*` 的 agent IDs 与 `hivemind-default-*:local` 的 image tags。启动前请先构建这些镜像。

当 `HIVEMIND_AUTOLOAD_DEFAULT_AGENTS=true` 时，启动会根据配置的 default image 字段按 ID upsert 默认 agents。这样在重置 DB 后也能保持稳定 ID，`.env` 无需每次改 UUID。
如果配置的默认镜像缺失，启动会直接 fail fast。

生成一个加密 key：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 上传 Agents

Agent 就是 Docker 容器。你可以把源文件打成 tarball 上传——由服务端构建镜像：

```bash
# 创建一个包含 Dockerfile 的 agent 源码目录
mkdir my-agent && cd my-agent
cat > Dockerfile <<'EOF'
FROM python:3.12-slim
RUN pip install httpx
COPY . /app
WORKDIR /app
CMD ["python", "agent.py"]
EOF
cat > agent.py <<'EOF'
import os, httpx
# ... 你的 agent 逻辑：使用 BRIDGE_URL 与 SESSION_TOKEN ...
print("Agent output goes to stdout")
EOF

# 打包并上传
tar czf ../agent.tar.gz .
curl -X POST http://localhost:8100/v1/agents/upload \
  -F "name=my-agent" \
  -F "archive=@../agent.tar.gz"
# 返回：{"agent_id": "abc123", "name": "my-agent", "files_extracted": 2}
```

客户端不需要 Docker CLI。不需要 registry。不需要复杂的鉴权。

## Agent 角色（Roles）

所有 agents 都是 Docker 容器。核心定义了四种角色：

| 角色 | 目的 | 可用工具 | Bridge 额外能力 |
|------|---------|-----------------|---------------|
| **Index** | 从文档提取 index_text + metadata | search, read, list | — |
| **Scope** | 为 query 决定 record_id 白名单 | search, read, list（全量访问） | `/sandbox/simulate`、query-agent 文件检查（仅限同一次 query 使用的 query agent） |
| **Query** | 检索并回答问题 | search, read, list（受 scope 限制） | — |
| **Mediator** | 审计/过滤 query 输出 | 无 | — |

Agents 将输出写到 **stdout**，并以退出码 0 结束。

## 数据模型

```
┌─────────────────────── SQLite DB ───────────────────────┐
│                                                         │
│  records                          records_fts (FTS5)    │
│  ┌──────────────────────┐         ┌──────────────────┐  │
│  │ id         TEXT PK   │         │ index_text       │  │
│  │ data       TEXT      │◄────────│ (virtual table   │  │
│  │   (Fernet encrypted) │  rowid  │  over records)   │  │
│  │ metadata   TEXT      │         └──────────────────┘  │
│  │   (schemaless JSON)  │                               │
│  │ index_text TEXT      │         agents                │
│  │   (nullable, FTS)    │         ┌──────────────────┐  │
│  │ created_at REAL      │         │ agent_id     PK  │  │
│  └──────────────────────┘         │ name, image      │  │
│                                   │ memory_mb        │  │
│  agent_files                      │ max_llm_calls    │  │
│  ┌──────────────────────┐         │ max_tokens       │  │
│  │ agent_id   TEXT      │────────►│ timeout_seconds  │  │
│  │ file_path  TEXT      │         └──────────────────┘  │
│  │ content    TEXT      │                               │
│  │ size_bytes INT       │                               │
│  └──────────────────────┘                               │
└─────────────────────────────────────────────────────────┘
```

**会存什么：**
- `records.data` —— 加密后的密文（没有 `HIVEMIND_ENCRYPTION_KEY` 无法解密）
- `records.metadata` —— 无 schema 的 JSON（由应用定义）
- `records.index_text` —— 可被 FTS 检索的明文（可为空）

**通过 API 会输出什么：**
- agent 生成的答案（可选：由 mediator agent 审计）
- 记录的 metadata + index_text（通过 `GET /v1/admin/records/{id}`）——永远不会返回 raw data

## 数据库

SQLite + FTS5 全文检索，并启用 WAL 模式。

Alembic schema migrations 会在启动时自动运行。

```bash
# 手动迁移命令
uv run alembic -c alembic.ini upgrade head   # 升级到最新
uv run alembic -c alembic.ini current        # 查看当前 revision

# 直接检查
sqlite3 hivemind.db ".schema"
sqlite3 hivemind.db "SELECT id, metadata, index_text FROM records"
sqlite3 hivemind.db "SELECT * FROM records_fts WHERE records_fts MATCH 'migration'"
```

## 项目结构

```
hivemind/
  __init__.py          # Public API exports
  version.py           # Version resolution (from package metadata)
  config.py            # Settings (env vars)
  core.py              # Hivemind class — thin wrapper (store + pipeline + health)
  server.py            # FastAPI HTTP server
  models.py            # Pydantic request/response models
  store.py             # RecordStore — SQLite + FTS5 + Fernet encryption
  pipeline.py          # Pipeline orchestrator (store + query pipelines)
  tools.py             # Agent tools (search, read, list, agent file tools)
  migrations.py        # Alembic migration runner
  alembic/             # Alembic env + version scripts
  sandbox/
    __init__.py        # Sandbox exports
    models.py          # AgentConfig, SandboxSettings, bridge models, SimulateRequest/Response
    settings.py        # build_sandbox_settings() — maps app config to sandbox config
    budget.py          # Per-query budget tracking (calls + tokens)
    bridge.py          # Ephemeral HTTP bridge server (LLM proxy + tools + simulation)
    docker_runner.py   # DockerRunner — container lifecycle, image extraction, cleanup
    backend.py         # SandboxBackend (implements run() interface)
    agents.py          # Agent registration + source file storage (SQLite)
agents/
  default-index/       # Default index agent (Docker image)
  default-query/       # Default query agent (Docker image)
  default-scope/       # Default scope agent (Docker image)
  default-mediator/    # Default mediator agent (Docker image)
  examples/            # Example agents — ready to upload (see agents/examples/README.md)
    simple-query/      # Minimal search + synthesize
    tool-loop-query/   # Agentic loop with parallel tools + auto-compaction
    metadata-scope/    # Team-based access control
    redact-mediator/   # PII redaction
tests/
  conftest.py                # Shared fixtures (tmp_db)
  test_store.py              # RecordStore + encryption unit tests
  test_api.py                # FastAPI endpoint unit tests
  test_pipeline.py           # Pipeline orchestrator tests
  test_simulate.py           # Simulation + budget carving tests
  test_tools.py              # Agent tools + agent file inspection tools
  test_core_store.py         # Core integration tests
  test_migrations.py         # Alembic migration tests
  test_sandbox_budget.py     # Budget tracking tests
  test_sandbox_agents.py     # Agent CRUD + file storage tests
  test_sandbox_backend.py    # Sandbox backend tests
  test_sandbox_bridge.py     # Bridge server tests
  test_docker_runner.py      # Docker runner tests (mocked)
  test_integration_docker.py # Docker integration tests (real containers)
  fixtures/
    Dockerfile.test-agent    # Minimal test image for integration tests
```

## API 参考

完整的 API 文档（所有 endpoints、请求/响应 schema、示例）请看 `API.md`。

## 测试

```bash
# 单元测试
uv run pytest tests/ -q

# Lint
uv tool run ruff check .

# Docker 集成测试（需要 Docker + 测试镜像）
docker build -t hivemind-test-agent:latest -f tests/fixtures/Dockerfile.test-agent tests/fixtures/
uv run pytest tests/test_integration_docker.py -v
```
