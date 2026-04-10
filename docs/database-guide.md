# Hivemind PostgreSQL 数据库使用指南

本文档说明 hivemind-core 如何通过 SQL Proxy 连接 PostgreSQL，以及三方服务如何操作数据库。

---

## 1. 连接架构

hivemind-core 通过 SQL Proxy（HTTP 中间层）访问 PostgreSQL，不直接暴露数据库端口。

```
┌─────────────── App CVM ───────────────┐     ┌────────── Postgres CVM ──────────┐
│                                       │     │                                  │
│  hivemind-core                        │     │  sql-proxy (:8080)               │
│    │                                  │     │    │                             │
│    │ HttpDatabase                     │     │    │ psycopg                     │
│    │   POST /execute      ───────────────▶  │    │   直连                      │
│    │   POST /execute_commit ─────────────▶  │    ▼                             │
│    │   GET  /schema       ───────────────▶  │  PostgreSQL (:5432)              │
│    │                                  │     │    user: hivemind                │
│    │   Header: X-Proxy-Key           │     │    pass: ${DB_PASS}              │
│                                       │     │    db:   hivemind               │
└───────────────────────────────────────┘     └──────────────────────────────────┘
```

### 1.1 连接流程

```
python -m hivemind.server
    │
    ▼
Settings()                 ← 从环境变量 / .env 读取配置
    │                         HIVEMIND_DATABASE_URL (https://...)
    │                         HIVEMIND_SQL_PROXY_KEY
    ▼
Hivemind(settings)
    │
    ▼
db.connect(dsn, proxy_key)
    │
    └─ dsn 以 https:// 开头 ──▶ HttpDatabase(url, proxy_key)
                                     │
                                     ├─ httpx.Client(base_url=url, headers={"X-Proxy-Key": key})
                                     ├─ _bootstrap()  ← 通过 HTTP 自动建内部表
                                     └─ 返回 HttpDatabase 实例
```

### 1.2 环境变量

| 环境变量 | 说明 | 示例 |
|---|---|---|
| `HIVEMIND_DATABASE_URL` | SQL Proxy 地址 | `https://<pg_cvm_id>-8080.app.phala.network` |
| `HIVEMIND_SQL_PROXY_KEY` | SQL Proxy 认证密钥 | `VG3K2CEuxltGFbdg...` |
| `HIVEMIND_API_KEY` | hivemind-core HTTP API 认证密钥 | `your-api-key` |

`.env` 配置：

```bash
HIVEMIND_DATABASE_URL=https://<pg_cvm_id>-8080.app.phala.network
HIVEMIND_SQL_PROXY_KEY=your-proxy-shared-secret
HIVEMIND_API_KEY=your-api-key
```

### 1.3 连接代码

`hivemind/db.py` — 路由函数根据 DSN scheme 选择连接方式：

```python
def connect(dsn: str, proxy_key: str = "") -> Database | HttpDatabase:
    if dsn.startswith("http://") or dsn.startswith("https://"):
        return HttpDatabase(dsn, proxy_key=proxy_key)
    return Database(dsn)
```

`HttpDatabase` 通过 httpx 将 SQL 请求发送到 SQL Proxy：

```python
class HttpDatabase:
    def __init__(self, base_url: str, proxy_key: str = ""):
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-Proxy-Key": proxy_key} if proxy_key else {},
            timeout=30.0,
        )
        self._bootstrap()  # 自动建内部表

    def execute(self, sql, params=None) -> list[dict]:
        resp = self._client.post("/execute", json={"sql": sql, "params": list(params) if params else None})
        return self._check(resp)["rows"]

    def execute_commit(self, sql, params=None) -> int:
        resp = self._client.post("/execute_commit", json={"sql": sql, "params": list(params) if params else None})
        return self._check(resp)["rowcount"]

    def get_schema(self, exclude_internal=True) -> list[dict]:
        qs = "" if exclude_internal else "?exclude_internal=false"
        resp = self._client.get(f"/schema{qs}")
        return self._check(resp)["rows"]
```

### 1.4 SQL Proxy 端

`deploy/postgres/sql_proxy.py` — 轻量 HTTP 服务，将 HTTP 请求转为 psycopg 直连 PostgreSQL：

```python
DB_DSN = os.environ.get("DATABASE_URL", "postgresql://hivemind:hivemind@localhost:5432/hivemind")
PROXY_PORT = int(os.environ.get("SQL_PROXY_PORT", "8080"))
PROXY_KEY = os.environ.get("SQL_PROXY_KEY", "")
```

SQL Proxy 的 Docker Compose 配置（`deploy/phala/docker-compose.postgres.yaml`）：

```yaml
services:
  db:
    image: ghcr.io/account-link/hivemind-postgres:latest
    environment:
      POSTGRES_DB: hivemind
      POSTGRES_USER: hivemind
      POSTGRES_PASSWORD: ${DB_PASS:?DB_PASS must be set}

  sql-proxy:
    image: ghcr.io/account-link/hivemind-sql-proxy:latest
    environment:
      DATABASE_URL: "postgresql://hivemind:${DB_PASS}@db:5432/hivemind"
      SQL_PROXY_KEY: ${SQL_PROXY_KEY:?SQL_PROXY_KEY must be set}
      SQL_PROXY_PORT: "8080"
    ports:
      - "8080:8080"
    depends_on:
      db: { condition: service_healthy }
```

App CVM 配置（`deploy/phala/docker-compose.core.yaml`）：

```yaml
services:
  hivemind:
    environment:
      HIVEMIND_DATABASE_URL: ${HIVEMIND_DATABASE_URL:?Set to SQL proxy URL}
      HIVEMIND_SQL_PROXY_KEY: ${SQL_PROXY_KEY:?Set SQL proxy shared secret}
```

### 1.5 Bootstrap（自动建表）

连接成功后自动创建 3 张内部表（`CREATE TABLE IF NOT EXISTS`）：

```
_hivemind_agents       — Agent 注册信息
_hivemind_agent_files  — Agent 源码文件
_hivemind_query_runs   — Query 执行记录
```

并通过 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 做向前兼容的 migration。

### 1.6 Hivemind 初始化流程

```python
# hivemind/core.py
class Hivemind:
    def __init__(self, settings: Settings):
        # 1. 连接数据库（通过 SQL Proxy）
        self.db = connect(settings.database_url, proxy_key=settings.sql_proxy_key)

        # 2. 初始化 store
        self.agent_store = AgentStore(self.db)   # Agent CRUD
        self.run_store = RunStore(self.db)        # Query Run CRUD

        # 3. 注册默认 Agent（如果配置了）
        self._bootstrap_default_agents()

        # 4. 初始化 Pipeline（处理 query/index 请求）
        self.pipeline = Pipeline(settings, self.db, self.agent_store)
```

---

## 2. SQL Proxy 端点参考

三方服务可直接调用 SQL Proxy 操作数据库。

### 连接参数

| 参数 | 值 |
|---|---|
| **URL** | `https://<pg_cvm_id>-8080.app.phala.network` |
| **认证 Header** | `X-Proxy-Key: <SQL_PROXY_KEY>` |

### 端点列表

| Method | Path | 说明 | 请求体 | 响应体 |
|---|---|---|---|---|
| `GET` | `/health` | 健康检查（无需认证） | — | `{"status": "ok"}` |
| `POST` | `/execute` | SELECT 查询 | `{"sql": "...", "params": [...]}` | `{"rows": [...]}` |
| `POST` | `/execute_commit` | 写操作 (INSERT/UPDATE/DELETE/DDL) | `{"sql": "...", "params": [...]}` | `{"rowcount": N}` |
| `GET` | `/schema` | 获取表结构 | — | `{"rows": [...]}` |
| `POST` | `/import/sql` | 导入 SQL dump（多语句事务） | `{"sql": "..."}` 或纯文本 | `{"statements_executed": N}` |
| `POST` | `/import/csv` | 导入 CSV 数据 | `{"table":"...", "data":"...", "header":true}` | `{"rows_imported": N}` |

### 请求格式

```json
{
  "sql": "SQL 语句，使用 %s 作为参数占位符",
  "params": ["参数1", "参数2"]
}
```

- `sql` (string, 必填): SQL 语句
- `params` (array, 可选): 参数列表

---

## 3. 表操作 (DDL)

```bash
PROXY_URL="https://<pg_cvm_id>-8080.app.phala.network"
PROXY_KEY="your-proxy-key"

# 建表
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE, team TEXT, created_at TIMESTAMP DEFAULT NOW())"}'

# 加列
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT"}'

# 删列
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "ALTER TABLE users DROP COLUMN IF EXISTS phone"}'

# 建索引
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "CREATE INDEX IF NOT EXISTS idx_users_team ON users(team)"}'

# 删表
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "DROP TABLE IF EXISTS users"}'
```

---

## 4. 数据操作 (CRUD)

### INSERT

```bash
# 单条插入
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "INSERT INTO users (name, email, team) VALUES (%s, %s, %s)",
    "params": ["Alice", "alice@example.com", "backend"]
  }'
# → {"rowcount": 1}

# 批量插入
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "INSERT INTO users (name, email, team) VALUES (%s, %s, %s), (%s, %s, %s)",
    "params": ["Alice", "alice@example.com", "backend", "Bob", "bob@example.com", "frontend"]
  }'
# → {"rowcount": 2}

# UPSERT（冲突时更新）
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "INSERT INTO users (name, email, team) VALUES (%s, %s, %s) ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, team = EXCLUDED.team",
    "params": ["Alice Smith", "alice@example.com", "platform"]
  }'
```

### SELECT

```bash
# 条件查询
curl -X POST "$PROXY_URL/execute" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "SELECT * FROM users WHERE team = %s ORDER BY created_at DESC LIMIT 10",
    "params": ["backend"]
  }'
# → {"rows": [{"id": 1, "name": "Alice", "email": "alice@example.com", ...}]}

# 聚合查询
curl -X POST "$PROXY_URL/execute" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT team, COUNT(*) as cnt FROM users GROUP BY team ORDER BY cnt DESC"}'
# → {"rows": [{"team": "backend", "cnt": 5}, ...]}
```

### UPDATE

```bash
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "UPDATE users SET team = %s WHERE email = %s",
    "params": ["platform", "alice@example.com"]
  }'
# → {"rowcount": 1}
```

### DELETE

```bash
curl -X POST "$PROXY_URL/execute_commit" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "DELETE FROM users WHERE email = %s",
    "params": ["alice@example.com"]
  }'
# → {"rowcount": 1}
```

---

## 5. 查询 Schema

```bash
curl "$PROXY_URL/schema" -H "X-Proxy-Key: $PROXY_KEY"
```

返回所有用户表的列信息（自动排除 `_hivemind_*` 内部表）：

```json
{
  "rows": [
    {"table_name": "users", "column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": "nextval(...)"},
    {"table_name": "users", "column_name": "name", "data_type": "text", "is_nullable": "NO", "column_default": null}
  ]
}
```

如需包含内部表：`GET /schema?exclude_internal=false`

---

## 6. 批量数据导入

### 导入 SQL Dump

```bash
# JSON 格式
curl -X POST "$PROXY_URL/import/sql" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "CREATE TABLE test (id SERIAL PRIMARY KEY, name TEXT); INSERT INTO test (name) VALUES ('\''a'\''), ('\''b'\'');"}'
# → {"statements_executed": 2}

# 纯文本 SQL 文件
curl -X POST "$PROXY_URL/import/sql" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: text/plain" \
  --data-binary @dump.sql
# → {"statements_executed": N}
```

所有语句在同一个事务中执行，任一失败则全部回滚。

### 导入 CSV

```bash
curl -X POST "$PROXY_URL/import/csv" \
  -H "X-Proxy-Key: $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "table": "users",
    "data": "name,email,team\nAlice,alice@example.com,backend\nBob,bob@example.com,frontend",
    "delimiter": ",",
    "header": true,
    "columns": ["name", "email", "team"]
  }'
# → {"rows_imported": 2}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `table` | string | 是 | 目标表名 |
| `data` | string | 是 | CSV 内容 |
| `delimiter` | string | 否 | 分隔符，默认 `,` |
| `header` | bool | 否 | 首行是否为表头，默认 `true` |
| `columns` | string[] | 否 | 指定列名（不指定则用表头或全部列） |

### 使用导入脚本

```bash
cd deploy/postgres

# 导入 SQL 文件
SQL_PROXY_URL="https://..." SQL_PROXY_KEY="..." ./import-data.sh sql dump.sql

# 导入 CSV 到指定表
SQL_PROXY_URL="https://..." SQL_PROXY_KEY="..." ./import-data.sh csv users users.csv
```

---

## 7. Python 客户端封装示例

```python
import httpx


class HivemindDB:
    """通过 SQL Proxy 操作 Hivemind 数据库"""

    def __init__(self, proxy_url: str, proxy_key: str):
        self._client = httpx.Client(
            base_url=proxy_url,
            headers={"X-Proxy-Key": proxy_key},
            timeout=30,
        )

    def query(self, sql: str, params: list = None) -> list[dict]:
        """SELECT 查询，返回行列表"""
        resp = self._client.post("/execute", json={"sql": sql, "params": params or []})
        resp.raise_for_status()
        return resp.json()["rows"]

    def execute(self, sql: str, params: list = None) -> int:
        """写操作 (INSERT/UPDATE/DELETE/DDL)，返回影响行数"""
        resp = self._client.post("/execute_commit", json={"sql": sql, "params": params or []})
        resp.raise_for_status()
        return resp.json()["rowcount"]

    def schema(self) -> list[dict]:
        """获取表结构"""
        resp = self._client.get("/schema")
        resp.raise_for_status()
        return resp.json()["rows"]

    def import_sql(self, sql_dump: str) -> int:
        """导入 SQL dump，返回执行语句数"""
        resp = self._client.post("/import/sql", json={"sql": sql_dump})
        resp.raise_for_status()
        return resp.json()["statements_executed"]

    def import_csv(self, table: str, csv_data: str, header: bool = True) -> int:
        """导入 CSV 数据，返回导入行数"""
        resp = self._client.post("/import/csv", json={"table": table, "data": csv_data, "header": header})
        resp.raise_for_status()
        return resp.json()["rows_imported"]

    def close(self):
        self._client.close()


# ── 使用 ──

db = HivemindDB(
    proxy_url="https://<pg_cvm_id>-8080.app.phala.network",
    proxy_key="your-proxy-key",
)

# 建表
db.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        id SERIAL PRIMARY KEY,
        content TEXT NOT NULL,
        team TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )
""")

# 插入
db.execute(
    "INSERT INTO notes (content, team) VALUES (%s, %s)",
    ["Sprint retro: migrated to gRPC", "backend"],
)

# 查询
rows = db.query("SELECT * FROM notes WHERE team = %s", ["backend"])
for r in rows:
    print(r["id"], r["content"])

# 查看所有表
for col in db.schema():
    print(f"{col['table_name']}.{col['column_name']} ({col['data_type']})")

db.close()
```

---

## 8. 内部表说明

Hivemind 自动维护 3 张内部表（`_hivemind_` 前缀），三方服务**不应操作**：

| 表 | 用途 |
|---|---|
| `_hivemind_agents` | Agent 注册（ID、名称、类型、资源限制） |
| `_hivemind_agent_files` | Agent 源码文件 |
| `_hivemind_query_runs` | Query 执行记录（状态、各阶段耗时、输出） |

---

## 9. 注意事项

1. **占位符**: 使用 `%s`（psycopg 风格），不要拼接 SQL 字符串
2. **读写分离**: SELECT 用 `POST /execute`，写操作用 `POST /execute_commit`
3. **事务**: `/execute` 自动 rollback（只读），`/execute_commit` 自动 commit，`/import/sql` 整体事务
4. **内部表**: `_hivemind_*` 表不要直接修改
5. **认证**: 所有端点（除 `/health`）需要 `X-Proxy-Key` header
6. **PostgreSQL 版本**: 16 (Alpine)
