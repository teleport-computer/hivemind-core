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


# ── per-file attestable flag ──────────────────────────────────────────


def test_default_all_attestable_digests_match(fresh_db):
    """No private_paths → attested digest equals total digest."""
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_default_att")
    store.upsert(cfg)
    store.save_files(
        cfg.agent_id,
        {"a.py": "x\n", "b.py": "y\n"},
    )

    digests = store.compute_digests(cfg.agent_id)
    assert digests["files_count"] == 2
    assert digests["attested_files_count"] == 2
    assert digests["files_digest"] == digests["attested_files_digest"]
    assert digests["files_digest"]  # non-empty


def test_private_path_excluded_from_attested_digest(fresh_db):
    """Files marked private contribute to ``files_digest`` but not
    ``attested_files_digest``. The attested digest equals the digest
    over only the public files — what a recipient verifies against
    published source without holding the secret."""
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_private")
    store.upsert(cfg)
    public_files = {"a.py": "public_a\n", "b.py": "public_b\n"}
    store.save_files(
        cfg.agent_id,
        {**public_files, "prompt.md": "SECRET RULES\n"},
        private_paths=["prompt.md"],
    )

    d_full = store.compute_digests(cfg.agent_id)
    assert d_full["files_count"] == 3
    assert d_full["attested_files_count"] == 2
    assert d_full["files_digest"] != d_full["attested_files_digest"]

    # Recompute the attested digest from a clean store with ONLY the
    # public files. It must match d_full["attested_files_digest"] —
    # proving B can verify without holding the private file.
    store2 = AgentStore(db, sealer=None, tenant_id=None)
    cfg2 = _config("agent_public_only")
    store2.upsert(cfg2)
    store2.save_files(cfg2.agent_id, public_files)
    d_pub = store2.compute_digests(cfg2.agent_id)
    assert d_pub["files_digest"] == d_full["attested_files_digest"]


def test_changing_private_content_does_not_affect_attested_digest(fresh_db):
    """Mutating the private file leaves ``attested_files_digest`` stable —
    the security claim is "public surface unchanged" even as private
    content rotates (e.g. .env credential rotation)."""
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_rot")
    store.upsert(cfg)
    public = {"a.py": "x\n"}

    store.save_files(
        cfg.agent_id,
        {**public, ".env": "API_KEY=v1\n"},
        private_paths=[".env"],
    )
    d1 = store.compute_digests(cfg.agent_id)

    store.replace_files(
        cfg.agent_id,
        {**public, ".env": "API_KEY=v2_rotated\n"},
        private_paths=[".env"],
    )
    d2 = store.compute_digests(cfg.agent_id)

    assert d1["attested_files_digest"] == d2["attested_files_digest"]
    assert d1["files_digest"] != d2["files_digest"]


def test_list_file_paths_surfaces_attestable_flag(fresh_db):
    """``list_file_paths`` exposes per-file attestable so the CLI can
    show ``[private]`` markers in the attest output."""
    db, _ = fresh_db
    store = AgentStore(db, sealer=None, tenant_id=None)
    cfg = _config("agent_list")
    store.upsert(cfg)
    store.save_files(
        cfg.agent_id,
        {"public.py": "p\n", "secret.txt": "s\n"},
        private_paths=["secret.txt"],
    )
    listing = {f["path"]: f["attestable"] for f in store.list_file_paths(cfg.agent_id)}
    assert listing == {"public.py": True, "secret.txt": False}


def test_attestable_under_seal(fresh_db):
    """Per-file flag works under seal: ciphertext on disk, attested
    digest computed over the *plaintext* (decrypted on read), private
    file still excluded."""
    db, _ = fresh_db
    sealer = TenantSealer()
    tenant_id = "t_seal_att"
    sealer.cache(tenant_id, new_dek())
    store = AgentStore(db, sealer=sealer, tenant_id=tenant_id)
    cfg = _config("agent_seal_att")
    store.upsert(cfg)
    store.save_files(
        cfg.agent_id,
        {"a.py": "code\n", "prompt.md": "SECRET\n"},
        private_paths=["prompt.md"],
    )

    # On-disk: both rows are ciphertext.
    rows = db.execute(
        "SELECT file_path, content, ciphertext, attestable "
        "FROM _hivemind_agent_files WHERE agent_id = %s "
        "ORDER BY file_path",
        [cfg.agent_id],
    )
    by_path = {r["file_path"]: r for r in rows}
    assert by_path["a.py"]["ciphertext"] is not None
    assert by_path["prompt.md"]["ciphertext"] is not None
    assert by_path["a.py"]["attestable"] is True
    assert by_path["prompt.md"]["attestable"] is False

    digests = store.compute_digests(cfg.agent_id)
    assert digests["files_count"] == 2
    assert digests["attested_files_count"] == 1
    assert digests["files_digest"] != digests["attested_files_digest"]
