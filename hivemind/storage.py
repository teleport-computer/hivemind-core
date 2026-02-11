import json
import sqlite3

from cryptography.fernet import Fernet

from .models import Scope


class Storage:
    def __init__(self, db_path: str, encryption_key: str = ""):
        self.db_path = db_path
        self._fernet = Fernet(encryption_key.encode()) if encryption_key else None
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                space_id TEXT NOT NULL DEFAULT 'public',
                user_id TEXT,
                timestamp REAL NOT NULL,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS record_index (
                record_id TEXT PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags TEXT NOT NULL,
                key_claims TEXT NOT NULL DEFAULT '',
                extra TEXT NOT NULL DEFAULT '{}',
                timestamp REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_records_space ON records(space_id);
            CREATE INDEX IF NOT EXISTS idx_records_user ON records(user_id);
            CREATE INDEX IF NOT EXISTS idx_records_timestamp ON records(timestamp);
        """)
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='index_fts'"
        ).fetchone()
        if not row:
            self._conn.execute("""
                CREATE VIRTUAL TABLE index_fts USING fts5(
                    title, summary, tags, key_claims,
                    content=record_index, content_rowid=rowid
                )
            """)
        self._conn.commit()

    def _apply_scope(
        self, sql: str, params: list, scope: Scope, record_alias: str = "r"
    ) -> tuple[str, list]:
        if scope.user_ids is not None:
            placeholders = ",".join("?" * len(scope.user_ids))
            sql += f" AND {record_alias}.user_id IN ({placeholders})"
            params.extend(scope.user_ids)
        if scope.record_ids is not None:
            placeholders = ",".join("?" * len(scope.record_ids))
            sql += f" AND {record_alias}.id IN ({placeholders})"
            params.extend(scope.record_ids)
        return sql, params

    # ── Encryption ──

    def _encrypt(self, plaintext: str) -> str:
        if not self._fernet:
            return plaintext
        return self._fernet.encrypt(plaintext.encode()).decode()

    def _decrypt(self, stored: str) -> str:
        if not self._fernet:
            return stored
        return self._fernet.decrypt(stored.encode()).decode()

    # ── Write ──

    def write_record(
        self,
        id: str,
        text: str,
        space_id: str,
        user_id: str | None,
        timestamp: float,
        metadata: dict | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO records (id, text, space_id, user_id, timestamp, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (id, self._encrypt(text), space_id, user_id, timestamp,
             json.dumps(metadata) if metadata else None),
        )
        self._conn.commit()

    def write_index(
        self,
        record_id: str,
        title: str,
        summary: str,
        tags: str,
        key_claims: str,
        extra: str,
        timestamp: float,
    ) -> None:
        self._conn.execute(
            "INSERT INTO record_index "
            "(record_id, title, summary, tags, key_claims, extra, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record_id, title, summary, tags, key_claims, extra, timestamp),
        )
        rowid = self._conn.execute(
            "SELECT rowid FROM record_index WHERE record_id = ?", (record_id,)
        ).fetchone()[0]
        self._conn.execute(
            "INSERT INTO index_fts(rowid, title, summary, tags, key_claims) "
            "VALUES (?, ?, ?, ?, ?)",
            (rowid, title, summary, tags, key_claims),
        )
        self._conn.commit()

    def update_index(
        self,
        record_id: str,
        title: str,
        summary: str,
        tags: str,
        key_claims: str,
        extra: str,
    ) -> bool:
        row = self._conn.execute(
            "SELECT rowid, title, summary, tags, key_claims "
            "FROM record_index WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if not row:
            return False
        rowid = row[0]
        # Delete old FTS entry
        self._conn.execute(
            "INSERT INTO index_fts(index_fts, rowid, title, summary, tags, key_claims) "
            "VALUES ('delete', ?, ?, ?, ?, ?)",
            (rowid, row[1], row[2], row[3], row[4]),
        )
        # Update record_index
        self._conn.execute(
            "UPDATE record_index "
            "SET title=?, summary=?, tags=?, key_claims=?, extra=? "
            "WHERE record_id=?",
            (title, summary, tags, key_claims, extra, record_id),
        )
        # Insert new FTS entry
        self._conn.execute(
            "INSERT INTO index_fts(rowid, title, summary, tags, key_claims) "
            "VALUES (?, ?, ?, ?, ?)",
            (rowid, title, summary, tags, key_claims),
        )
        self._conn.commit()
        return True

    # ── Scoped reads ──

    def search_index(
        self,
        query: str,
        scope: Scope,
        space_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        sql = (
            "SELECT ri.record_id, ri.title, ri.summary, ri.tags, "
            "ri.key_claims, ri.timestamp, r.user_id, r.space_id "
            "FROM index_fts "
            "JOIN record_index ri ON index_fts.rowid = ri.rowid "
            "JOIN records r ON ri.record_id = r.id "
            "WHERE index_fts MATCH ?"
        )
        params: list = [query]
        if space_id:
            sql += " AND r.space_id = ?"
            params.append(space_id)
        sql, params = self._apply_scope(sql, params, scope)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except Exception:
            # FTS5 query syntax error (e.g. unbalanced quotes, invalid operators)
            return []
        return [dict(row) for row in rows]

    def read_record(self, record_id: str, scope: Scope) -> dict | None:
        sql = (
            "SELECT id, text, space_id, user_id, timestamp, metadata "
            "FROM records r WHERE r.id = ?"
        )
        params: list = [record_id]
        sql, params = self._apply_scope(sql, params, scope)
        row = self._conn.execute(sql, params).fetchone()
        if not row:
            return None
        result = dict(row)
        result["text"] = self._decrypt(result["text"])
        return result

    def list_index(
        self,
        scope: Scope,
        space_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        sql = (
            "SELECT ri.record_id, ri.title, ri.summary, ri.tags, "
            "ri.key_claims, ri.timestamp, r.user_id, r.space_id "
            "FROM record_index ri "
            "JOIN records r ON ri.record_id = r.id "
            "WHERE 1=1"
        )
        params: list = []
        if space_id:
            sql += " AND r.space_id = ?"
            params.append(space_id)
        sql, params = self._apply_scope(sql, params, scope)
        sql += " ORDER BY ri.timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_by_user(
        self,
        user_id: str,
        scope: Scope,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        sql = (
            "SELECT ri.record_id, ri.title, ri.summary, ri.tags, "
            "ri.key_claims, ri.timestamp, r.user_id, r.space_id "
            "FROM record_index ri "
            "JOIN records r ON ri.record_id = r.id "
            "WHERE r.user_id = ?"
        )
        params: list = [user_id]
        sql, params = self._apply_scope(sql, params, scope)
        sql += " ORDER BY ri.timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_users(self, scope: Scope) -> list[dict]:
        """List distinct user_ids with record counts."""
        sql = (
            "SELECT r.user_id, COUNT(*) as record_count "
            "FROM records r "
            "WHERE r.user_id IS NOT NULL"
        )
        params: list = []
        sql, params = self._apply_scope(sql, params, scope)
        sql += " GROUP BY r.user_id ORDER BY record_count DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ── Admin ──

    def count_records(self, space_id: str | None = None) -> int:
        if space_id:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM records WHERE space_id = ?", (space_id,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()
        return row[0]

    def delete_record(self, record_id: str) -> bool:
        row = self._conn.execute(
            "SELECT rowid, title, summary, tags, key_claims "
            "FROM record_index WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row:
            self._conn.execute(
                "INSERT INTO index_fts"
                "(index_fts, rowid, title, summary, tags, key_claims) "
                "VALUES ('delete', ?, ?, ?, ?, ?)",
                (row[0], row[1], row[2], row[3], row[4]),
            )
        cursor = self._conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def list_spaces(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT space_id, COUNT(*) as count "
            "FROM records GROUP BY space_id ORDER BY count DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self):
        self._conn.close()
