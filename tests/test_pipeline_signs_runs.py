"""Phase 5: pipeline builds a signed attestation envelope at completion.

These tests exercise ``Pipeline._build_run_attestation`` directly (no
Docker, no LLM) by patching ``hivemind.attestation`` state with a
deterministic Ed25519 keypair. We assert:

1. The envelope round-trips through the verifier.
2. The body's ``output_hash`` commits to the actual output.
3. When the run signer isn't bootstrapped, ``_build_run_attestation``
   returns ``None`` (no signature → run row has NULL ``attestation``).
"""

from __future__ import annotations

import base64
import hashlib
import os

import psycopg
import pytest

from hivemind import attestation as _att
from hivemind.config import Settings
from hivemind.core import Hivemind
from hivemind.run_signer import derive_run_signer

TEST_DSN = os.environ.get(
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
    not _pg_reachable(TEST_DSN),
    reason=f"Postgres not reachable at {TEST_DSN}",
)


class _FakeKeyResp:
    def __init__(self, key: bytes):
        self.key = key


class _FakeDstack:
    def get_key(self, path: str, _purpose: str):
        return _FakeKeyResp(b"\x77" * 32)


@pytest.fixture
def signer_state():
    """Install a deterministic run signer into hivemind.attestation."""
    priv, pub = derive_run_signer(_FakeDstack())
    saved = {
        "priv": _att._state.get("run_signer_priv"),
        "pub": _att._state.get("run_signer_pub"),
        "att": _att._state.get("attestation"),
        "ready": _att._state.get("ready"),
    }
    _att._state["run_signer_priv"] = priv
    _att._state["run_signer_pub"] = pub
    _att._state["ready"] = True
    _att._state["attestation"] = {"compose_hash": "deadbeef" * 8}
    yield priv, pub
    _att._state["run_signer_priv"] = saved["priv"]
    _att._state["run_signer_pub"] = saved["pub"]
    _att._state["attestation"] = saved["att"]
    _att._state["ready"] = saved["ready"]


@pytest.fixture
def hive():
    """Spin up a fresh Hivemind on a unique throwaway DB."""
    import secrets
    db_name = f"hm_p5_{secrets.token_hex(4)}"
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    parsed = psycopg.conninfo.conninfo_to_dict(TEST_DSN)
    parsed["dbname"] = db_name
    dsn = psycopg.conninfo.make_conninfo(**parsed)
    settings = Settings(
        database_url=dsn,
        admin_key="x",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
    )
    hm = Hivemind(settings)
    yield hm
    try:
        hm.db.close()
    except Exception:
        pass
    try:
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    except Exception:
        pass


def test_build_run_attestation_round_trips(hive, signer_state):
    priv, pub = signer_state
    envelope = hive.pipeline._build_run_attestation(
        run_id="run-abc123",
        status="completed",
        query_agent_id="qa-1",
        scope_agent_id="scope-1",
        prompt="how many docs?",
        output="42 docs",
        error=None,
    )
    assert envelope is not None
    assert envelope["body"]["run_id"] == "run-abc123"
    assert envelope["body"]["status"] == "completed"
    assert envelope["body"]["compose_hash"] == "deadbeef" * 8
    assert envelope["body"]["query_agent_id"] == "qa-1"
    assert envelope["body"]["scope_agent_id"] == "scope-1"
    assert envelope["body"]["output_hash"] == hashlib.sha256(
        b"42 docs"
    ).hexdigest()
    assert envelope["body"]["prompt_hash"] == hashlib.sha256(
        b"how many docs?"
    ).hexdigest()
    assert envelope["signer_pubkey_b64"] == base64.b64encode(pub).decode("ascii")

    # Signature verifies.
    from hivemind.run_signer import verify_payload
    sig = base64.b64decode(envelope["signature_b64"])
    assert verify_payload(pub, envelope["body"], sig) is True


def test_build_run_attestation_returns_none_when_signer_missing(hive):
    """Without a bootstrapped signer, the helper returns None (no
    signature is better than a fake one)."""
    saved = {
        "priv": _att._state.get("run_signer_priv"),
        "pub": _att._state.get("run_signer_pub"),
    }
    _att._state["run_signer_priv"] = None
    _att._state["run_signer_pub"] = None
    try:
        envelope = hive.pipeline._build_run_attestation(
            run_id="r",
            status="completed",
            query_agent_id="qa",
            scope_agent_id=None,
            prompt="p",
            output="o",
            error=None,
        )
        assert envelope is None
    finally:
        _att._state["run_signer_priv"] = saved["priv"]
        _att._state["run_signer_pub"] = saved["pub"]


def test_build_run_attestation_failed_run_includes_error_hash(hive, signer_state):
    envelope = hive.pipeline._build_run_attestation(
        run_id="r",
        status="failed",
        query_agent_id="qa",
        scope_agent_id="scope",
        prompt="p",
        output="",
        error="container OOM",
    )
    assert envelope["body"]["status"] == "failed"
    assert envelope["body"]["error_hash"] == hashlib.sha256(
        b"container OOM"
    ).hexdigest()
    assert envelope["body"]["output_hash"] == hashlib.sha256(b"").hexdigest()
