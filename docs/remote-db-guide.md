# 远程数据库操作指南

项目的 PostgreSQL 运行在 Phala CVM 中，通过 HTTP SQL Proxy 访问，不支持直接 `psql` 连接。

## 连接信息

| 参数 | 值 |
|------|-----|
| Proxy URL | `https://2181af2d134123a46613f62a0311dd1f5af984be-8080.dstack-pha-prod5.phala.network` |
| 认证方式 | `X-Proxy-Key` Header |
| Proxy Key | 见 `.env` 中的 `HIVEMIND_SQL_PROXY_KEY` |

为方便使用，先设置环境变量：

```bash
export DB_URL="https://2181af2d134123a46613f62a0311dd1f5af984be-8080.dstack-pha-prod5.phala.network"
export DB_KEY="VG3K2CEuxltGFbdgkds5mxZoLOhHomvNQHOi9bU8Etc"
```

---

## API 端点一览

| 端点 | 方法 | 用途 | 返回 |
|------|------|------|------|
| `/health` | GET | 健康检查（无需认证） | `{"status": "ok"}` |
| `/schema` | GET | 查看数据库结构 | `{"rows": [...]}` |
| `/execute` | POST | 只读查询 (SELECT) | `{"rows": [...]}` |
| `/execute_commit` | POST | 写操作 (INSERT/UPDATE/DELETE/DDL) | `{"rowcount": N}` |
| `/import/sql` | POST | 批量导入 SQL 文件 | `{"statements_executed": N}` |
| `/import/csv` | POST | 导入 CSV 数据 | `{"rows_imported": N}` |

---

## 常用操作示例

### 1. 健康检查

```bash
curl $DB_URL/health
```

### 2. 查看数据库 Schema

```bash
curl -s "$DB_URL/schema" \
  -H "X-Proxy-Key: $DB_KEY" | python3 -m json.tool
```

### 3. SELECT 查询

```bash
# 查询行数
curl -s -X POST "$DB_URL/execute" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT count(*) FROM data_xordi_tiktok_oauth_watch_history", "params": null}' \
  | python3 -m json.tool

# 带条件查询（使用 $1, $2 参数占位符）
curl -s -X POST "$DB_URL/execute" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT id, title, author FROM data_xordi_tiktok_oauth_watch_history WHERE author = $1 LIMIT 5", "params": ["treloch"]}' \
  | python3 -m json.tool

# 查看所有表
curl -s -X POST "$DB_URL/execute" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT table_name FROM information_schema.tables WHERE table_schema = '\''public'\'' ORDER BY table_name", "params": null}' \
  | python3 -m json.tool
```

### 4. INSERT 插入数据

```bash
curl -s -X POST "$DB_URL/execute_commit" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "INSERT INTO data_xordi_tiktok_oauth_watch_history (id, tiktok_account_id, video_id, title) VALUES ($1, $2, $3, $4)",
    "params": ["550e8400-e29b-41d4-a716-446655440000", "2507a998-bfe4-4baa-a607-f8b2c2326673", "12345", "test video"]
  }' | python3 -m json.tool
```

### 5. UPDATE 更新数据

```bash
curl -s -X POST "$DB_URL/execute_commit" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "UPDATE data_xordi_tiktok_oauth_watch_history SET title = $1 WHERE video_id = $2",
    "params": ["new title", "12345"]
  }' | python3 -m json.tool
```

### 6. DELETE 删除数据

```bash
curl -s -X POST "$DB_URL/execute_commit" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "DELETE FROM data_xordi_tiktok_oauth_watch_history WHERE video_id = $1",
    "params": ["12345"]
  }' | python3 -m json.tool
```

### 7. CREATE TABLE 建表

```bash
curl -s -X POST "$DB_URL/execute_commit" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "CREATE TABLE IF NOT EXISTS my_table (id uuid PRIMARY KEY, name text, created_at timestamptz DEFAULT now())",
    "params": null
  }' | python3 -m json.tool
```

### 8. DROP TABLE 删表

```bash
curl -s -X POST "$DB_URL/execute_commit" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "DROP TABLE IF EXISTS my_table", "params": null}' \
  | python3 -m json.tool
```

### 9. 批量导入 SQL 文件

```bash
curl -s -X POST "$DB_URL/import/sql" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: text/plain" \
  --data-binary @dump.sql | python3 -m json.tool
```

### 10. 导入 CSV 数据

```bash
curl -s -X POST "$DB_URL/import/csv" \
  -H "X-Proxy-Key: $DB_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "table": "my_table",
    "data": "id,name\n550e8400-e29b-41d4-a716-446655440000,alice",
    "delimiter": ",",
    "header": true
  }' | python3 -m json.tool
```

---

## 注意事项

1. **参数占位符**：使用 `$1`, `$2`, `$3`... 格式（psycopg 风格），不要用 `?` 或 `%s`
2. **读写分离**：SELECT 用 `/execute`，写操作（INSERT/UPDATE/DELETE/DDL）用 `/execute_commit`
3. **无需认证**：只有 `/health` 端点不需要 `X-Proxy-Key`
4. **SQL 导入**：`/import/sql` 支持多语句，会自动按分号拆分并在一个事务中执行
5. **错误返回**：失败时返回 `{"error": "错误信息"}`，HTTP 状态码 >= 400
