"""Round-trip tests for :class:`AgentStore` under the seal.

Verifies that with a sealer bound and the per-tenant DEK cached,
``save_files`` writes ciphertext to ``_hivemind_agent_files`` and
``read_file`` / ``get_files`` decrypt transparently. Also verifies
the legacy plaintext path still works (no sealer).

Postgres-backed; skips when ``HIVEMIND_TEST_DATABASE_URL`` not set.
"""

from __future__ import annotations

import os
import secrets

import psycopg
import pytest

from hivemind.db import Database
from hivemind.sandbox.agents import AgentStore
from hivemind.sandbox.models import AgentConfig
from hivemind.seal import TenantSealer, new_dek


TEST_DSN_BASE = os.environ.get(
    "HIVEMIND_TEST_DATABASE_URL",
    "postgresql://hivemind:dev@localhost:5432/postgres",
)


def _pg_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(TEST_DSN_BASE),
    reason=f"Postgres not reachable at {TEST_DSN_BASE}",
)


@pytest.fixture
def fresh_db():
    """Spin up a fresh per-test Postgres DB and tear it down after."""
    db_name = f"hm_seal_{secrets.token_hex(4)}"
    with psycopg.connect(TEST_DSN_BASE, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    # Build a DSN pointing at the new DB.
    parts = TEST_DSN_BASE.rsplit("/", 1)
    base = parts[0] if len(parts) == 2 else TEST_DSN_BASE
    dsn = f"{base}/{db_name}"
    db = Database(dsn)
    try:
        yield db, db_name
    finally:
        try:
            db.close()
        except Exception:
            pass
        try:
            with psycopg.connect(TEST_DSN_BASE, autocommit=True) as conn:
                conn.execute(
                    f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'
                )
        except Exception:
            pass


def _config(agent_id: str = "agent_demo") -> AgentConfig:
    return AgentConfig(
        agent_id=agent_id,
        name="demo",
        description="",
        agent_type="query",
        image="hivemind/agent-demo:latest",
        entrypoint=None,
        memory_mb=64,
        max_llm_calls=1,
        max_tokens=1,
        timeout_seconds=10,
    )


def test_plaintext_path_when_no_sealer(fresh_db):
    """Without a sealer the store keeps plaintext rows (legacy mode)."""
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_plain")
    store.upsert(cfg)
    store.save_files(cfg.agent_id, {"a.py": "print('A')\n"})

    rows = db.execute(
        "SELECT content, ciphertext FROM _hivemind_agent_files "
        "WHERE agent_id = %s",
        [cfg.agent_id],
    )
    assert len(rows) == 1
    assert rows[0]["content"] == "print('A')\n"
    assert rows[0]["ciphertext"] is None
    assert store.read_file(cfg.agent_id, "a.py") == "print('A')\n"


def test_ciphertext_path_when_sealer_warm(fresh_db):
    """With a warmed sealer, content column is NULL and ciphertext is
    base64; reads decrypt transparently."""
    db, _ = fresh_db
    sealer = TenantSealer()
    tenant_id = "t_demo"
    sealer.cache(tenant_id, new_dek())
    store = AgentStore(db, sealer=sealer, tenant_id=tenant_id)
    cfg = _config("agent_sealed")
    store.upsert(cfg)
    store.save_files(
        cfg.agent_id,
        {"a.py": "print('A')\n", "lib/util.py": "def f(): pass\n"},
    )

    rows = db.execute(
        "SELECT file_path, content, ciphertext FROM _hivemind_agent_files "
        "WHERE agent_id = %s ORDER BY file_path",
        [cfg.agent_id],
    )
    assert len(rows) == 2
    for r in rows:
        assert r["content"] is None
        assert r["ciphertext"] is not None
        # ciphertext is base64 — must not contain the plaintext source.
        assert "print(" not in r["ciphertext"]
        assert "def f" not in r["ciphertext"]

    # Reads decrypt transparently.
    assert store.read_file(cfg.agent_id, "a.py") == "print('A')\n"
    files = store.get_files(cfg.agent_id)
    assert files == {"a.py": "print('A')\n", "lib/util.py": "def f(): pass\n"}


def test_decrypt_fails_when_dek_evicted(fresh_db):
    """Cold cache after writes must surface the seal: read raises."""
    from hivemind.seal import TenantSealed
    db, _ = fresh_db
    sealer = TenantSealer()
    tenant_id = "t_demo"
    sealer.cache(tenant_id, new_dek())
    store = AgentStore(db, sealer=sealer, tenant_id=tenant_id)
    cfg = _config("agent_sealed_evict")
    store.upsert(cfg)
    store.save_files(cfg.agent_id, {"a.py": "print('A')\n"})

    # Simulate restart-evicts-everything.
    sealer.evict(tenant_id)
    with pytest.raises(TenantSealed):
        store.read_file(cfg.agent_id, "a.py")


def test_replace_files_keeps_encryption_invariant(fresh_db):
    db, _ = fresh_db
    sealer = TenantSealer()
    tenant_id = "t_demo"
    sealer.cache(tenant_id, new_dek())
    store = AgentStore(db, sealer=sealer, tenant_id=tenant_id)
    cfg = _config("agent_replace")
    store.upsert(cfg)
    store.save_files(cfg.agent_id, {"a.py": "v1\n"})
    store.replace_files(cfg.agent_id, {"a.py": "v2\n", "b.py": "x\n"})

    rows = db.execute(
        "SELECT file_path, content, ciphertext FROM _hivemind_agent_files "
        "WHERE agent_id = %s ORDER BY file_path",
        [cfg.agent_id],
    )
    paths = [r["file_path"] for r in rows]
    assert paths == ["a.py", "b.py"]
    for r in rows:
        assert r["content"] is None
        assert r["ciphertext"] is not None
    files = store.get_files(cfg.agent_id)
    assert files == {"a.py": "v2\n", "b.py": "x\n"}
