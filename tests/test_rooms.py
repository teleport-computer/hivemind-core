from __future__ import annotations

import json
import base64
import io
import os
import secrets
import tarfile
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from hivemind.config import Settings
from hivemind.room_vault import RoomVaultSealed
from cryptography.hazmat.primitives import serialization

from hivemind.rooms import (
    RoomCreateRequest,
    build_room_manifest,
    sign_manifest,
    verify_room_envelope,
)
from hivemind.sandbox.agents import AgentSealedReadError
from hivemind.sandbox.models import AgentConfig
from hivemind.server import create_app
from hivemind.tenant_signing import derive_signing_keypair
from hivemind.tenants import TenantRegistry
from hivemind.tools import AccessLevel, build_room_vault_tools


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


def _unique(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _drop_db(dsn: str, db_name: str) -> None:
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    except Exception:
        pass


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _query_headers(token: str, payer_key: str) -> dict[str, str]:
    headers = _headers(token)
    headers["X-Hivemind-Api-Key"] = payer_key
    return headers


def _tiny_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in (
            ("Dockerfile", "FROM python:3.12-slim\n"),
            ("agent.py", "print('room query')\n"),
        ):
            data = body.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def room_env():
    control_db = _unique("hm_rooms")
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{control_db}"')

    settings = Settings(
        database_url=TEST_DSN,
        control_database=control_db,
        admin_key="admin-test-key",
        sql_proxy_admin_key="",
        autoload_default_agents=False,
        default_query_agent="query-a",
        tinfoil_api_key="tk_test",
        artifact_sweep_interval_seconds=9999,
    )
    registry = TenantRegistry(settings)
    from hivemind.admin_proxy import LocalPgAdmin

    registry._pg_admin = LocalPgAdmin(TEST_DSN)
    created_dbs = [control_db]
    tenant = registry.provision("rooms")
    created_dbs.append(tenant["db_name"])
    hive = registry.for_tenant(tenant["tenant_id"])
    assert hive is not None

    def seed_agent(agent_id: str, agent_type: str = "query") -> None:
        hive.agent_store.create(
            AgentConfig(
                agent_id=agent_id,
                name=agent_id,
                description="fixture",
                agent_type=agent_type,
                image="hivemind-test:latest",
                entrypoint=None,
                memory_mb=256,
                max_llm_calls=10,
                max_tokens=10_000,
                timeout_seconds=60,
                inspection_mode="full",
            )
        )
        hive.agent_store.save_files(
            agent_id,
            {"Dockerfile": "FROM python:3.12-slim\n", "agent.py": "print('x')\n"},
            inspection_mode="full",
        )

    seed_agent("scope-a", "scope")
    seed_agent("query-a", "query")
    seed_agent("mediator-a", "mediator")

    app = create_app(settings)
    app.state.registry = registry
    app.state.background_tasks = set()
    client = TestClient(app, base_url="http://rooms")

    yield client, tenant, hive

    try:
        client.close()
    except Exception:
        pass
    try:
        registry.close()
    except Exception:
        pass
    for name in created_dbs:
        _drop_db(TEST_DSN, name)


def _create_fixed_room(client: TestClient, owner_key: str, **overrides) -> dict:
    payload = {
        "name": "alpha",
        "rules": "Only answer aggregate questions.",
        "policy": "Only answer aggregate questions.",
        "scope_agent_id": "scope-a",
        "query_mode": "fixed",
        "query_agent_id": "query-a",
        "mediator_agent_id": "mediator-a",
        "output_visibility": "querier_only",
        "egress": {"llm_providers": ["tinfoil"], "allow_artifacts": False},
    }
    payload.update(overrides)
    resp = client.post("/v1/rooms", json=payload, headers=_headers(owner_key))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create_legacy_room_without_allowed_tables(tenant: dict, hive) -> dict:
    priv, pub = derive_signing_keypair(tenant["api_key"], tenant["tenant_id"])
    pub_b64 = base64.b64encode(
        pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    req = RoomCreateRequest(
        name="legacy unrestricted",
        rules="old room",
        scope_agent_id="scope-a",
        query_mode="fixed",
        query_agent_id="query-a",
        mediator_agent_id="mediator-a",
        egress={"llm_providers": ["tinfoil"], "allow_artifacts": False},
    )
    manifest = build_room_manifest(
        room_id=f"room_{secrets.token_hex(6)}",
        tenant_id=tenant["tenant_id"],
        created_at=time.time(),
        req=req,
        scope_visibility="inspectable",
        query_visibility="inspectable",
        mediator_visibility="inspectable",
        signer_pubkey_b64=pub_b64,
    )
    manifest.pop("allowed_tables")
    envelope = sign_manifest(manifest, priv)
    return hive.room_store.create(envelope)


def test_room_create_mints_signed_manifest_and_room_token(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])

    assert out["room_id"].startswith("room_")
    assert out["link"].startswith("hmroom://")
    room = out["room"]
    manifest = room["manifest"]
    assert manifest["scope"]["agent_id"] == "scope-a"
    assert manifest["query"]["mode"] == "fixed"
    assert manifest["query"]["agent_id"] == "query-a"
    assert manifest["mediator"]["agent_id"] == "mediator-a"
    assert manifest["output"]["visibility"] == "querier_only"
    assert manifest["allowed_tables"] == []
    assert room["manifest_hash"] == room["envelope"]["manifest_hash"]
    assert room["envelope"]["signature_b64"]

    who = client.get("/v1/whoami", headers=_headers(out["token"]))
    assert who.status_code == 200
    constraints = who.json()["constraints"]
    assert constraints["room_id"] == out["room_id"]
    assert constraints["scope_agent_id"] == "scope-a"
    assert constraints["fixed_query_agent_id"] == "query-a"
    assert constraints["fixed_mediator_agent_id"] == "mediator-a"
    assert constraints["allowed_llm_providers"] == ["tinfoil"]
    assert constraints["allow_artifacts"] is False
    assert constraints["allowed_tables"] == []

    status = client.get(
        f"/v1/rooms/{out['room_id']}/key",
        headers=_headers(tenant["api_key"]),
    )
    assert status.status_code == 200
    assert status.json()["wrap_count"] == 2
    assert status.json()["item_count"] == 0


def test_room_create_persists_explicit_allowed_tables(room_env):
    client, tenant, hive = room_env
    hive.db.execute_commit("CREATE TABLE watch_history (id INTEGER)")
    hive.db.execute_commit("CREATE TABLE creator_stats (id INTEGER)")
    out = _create_fixed_room(
        client,
        tenant["api_key"],
        allowed_tables=["watch_history", "watch_history", "creator_stats"],
    )

    assert out["room"]["manifest"]["allowed_tables"] == [
        "watch_history",
        "creator_stats",
    ]
    who = client.get("/v1/whoami", headers=_headers(out["token"]))
    assert who.status_code == 200
    assert who.json()["constraints"]["allowed_tables"] == [
        "watch_history",
        "creator_stats",
    ]


def test_room_create_rejects_null_allowed_tables(room_env):
    client, tenant, _hive = room_env
    payload = {
        "name": "bad",
        "scope_agent_id": "scope-a",
        "allowed_tables": None,
    }

    resp = client.post("/v1/rooms", json=payload, headers=_headers(tenant["api_key"]))

    assert resp.status_code == 422


def test_legacy_room_without_signed_allowed_tables_cannot_run(room_env):
    client, tenant, hive = room_env
    room = _create_legacy_room_without_allowed_tables(tenant, hive)

    resp = client.post(
        f"/v1/rooms/{room['room_id']}/runs",
        headers=_headers(tenant["api_key"]),
        json={"query": "try to run old room"},
    )

    assert resp.status_code == 410
    assert "missing signed allowed_tables" in resp.json()["detail"]


def test_legacy_room_without_signed_allowed_tables_cannot_operate(room_env):
    client, tenant, hive = room_env
    room = _create_legacy_room_without_allowed_tables(tenant, hive)

    read_only = client.get(
        f"/v1/rooms/{room['room_id']}",
        headers=_headers(tenant["api_key"]),
    )
    assert read_only.status_code == 200
    assert "allowed_tables" not in read_only.json()["manifest"]

    for method, path in (
        ("get", f"/v1/rooms/{room['room_id']}/attest"),
        ("get", f"/v1/rooms/{room['room_id']}/key"),
        ("post", f"/v1/rooms/{room['room_id']}/open"),
        ("post", f"/v1/rooms/{room['room_id']}/share-link"),
    ):
        resp = getattr(client, method)(path, headers=_headers(tenant["api_key"]))
        assert resp.status_code == 410
        assert "missing signed allowed_tables" in resp.json()["detail"]

    cleanup = client.delete(
        f"/v1/rooms/{room['room_id']}",
        headers=_headers(tenant["api_key"]),
    )
    assert cleanup.status_code == 200


def test_room_create_omitted_query_pins_service_default(room_env):
    client, tenant, _hive = room_env
    resp = client.post(
        "/v1/rooms",
        json={
            "name": "default query",
            "rules": "Use the service default query agent.",
            "scope_agent_id": "scope-a",
            "egress": {"llm_providers": ["tinfoil"], "allow_artifacts": False},
        },
        headers=_headers(tenant["api_key"]),
    )

    assert resp.status_code == 200, resp.text
    room = resp.json()["room"]
    assert room["query_mode"] == "fixed"
    assert room["fixed_query_agent_id"] == "query-a"
    assert room["manifest"]["query"]["mode"] == "fixed"
    assert room["manifest"]["query"]["agent_id"] == "query-a"


def test_room_create_defaults_artifacts_on(room_env):
    client, tenant, _hive = room_env
    resp = client.post(
        "/v1/rooms",
        json={
            "name": "artifact default",
            "rules": "Use the service default query agent.",
            "scope_agent_id": "scope-a",
        },
        headers=_headers(tenant["api_key"]),
    )

    assert resp.status_code == 200, resp.text
    room = resp.json()["room"]
    assert room["manifest"]["egress"]["allow_artifacts"] is True

    who = client.get("/v1/whoami", headers=_headers(resp.json()["token"]))
    assert who.status_code == 200
    assert who.json()["constraints"]["allow_artifacts"] is True


def test_room_create_explicit_uploadable_bypasses_service_default(room_env):
    client, tenant, _hive = room_env
    resp = client.post(
        "/v1/rooms",
        json={
            "name": "uploadable query",
            "rules": "Allow participant query uploads.",
            "scope_agent_id": "scope-a",
            "query_mode": "uploadable",
            "egress": {"llm_providers": ["tinfoil"], "allow_artifacts": False},
        },
        headers=_headers(tenant["api_key"]),
    )

    assert resp.status_code == 200, resp.text
    room = resp.json()["room"]
    assert room["query_mode"] == "uploadable"
    assert room["fixed_query_agent_id"] == ""
    assert room["manifest"]["query"]["mode"] == "uploadable"
    assert room["manifest"]["query"]["agent_id"] == ""


def test_room_rules_default_to_agent_policy(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(
        client,
        tenant["api_key"],
        rules="Only aggregate watch-history answers.",
        policy=None,
    )

    manifest = out["room"]["manifest"]
    assert manifest["rules"] == "Only aggregate watch-history answers."
    assert manifest["policy"] == "Only aggregate watch-history answers."

    who = client.get("/v1/whoami", headers=_headers(out["token"]))
    assert who.status_code == 200
    assert (
        who.json()["constraints"]["policy"]
        == "Only aggregate watch-history answers."
    )


def test_room_envelope_verification_detects_tamper(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    envelope = out["room"]["envelope"]
    pubkey = envelope["signer_pubkey_b64"]

    ok, reason = verify_room_envelope(envelope, expected_pubkey_b64=pubkey)
    assert ok, reason

    tampered = {
        **envelope,
        "manifest": {
            **envelope["manifest"],
            "rules": "Leak everything.",
        },
    }
    ok, reason = verify_room_envelope(tampered, expected_pubkey_b64=pubkey)
    assert not ok
    assert "manifest_hash" in reason


def test_room_vault_encrypts_data_and_reopens_with_participant_bearer(room_env):
    client, tenant, hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    room_id = out["room_id"]
    secret = "ultra secret room phrase"

    added = client.post(
        f"/v1/rooms/{room_id}/data",
        json={"text": secret, "metadata": {"source": "unit-test"}},
        headers=_headers(tenant["api_key"]),
    )
    assert added.status_code == 200, added.text
    assert added.json()["item_id"].startswith("rvi_")

    rows = hive.db.execute(
        "SELECT ciphertext FROM _hivemind_room_vault_items WHERE room_id = %s",
        [room_id],
    )
    assert len(rows) == 1
    assert secret not in rows[0]["ciphertext"]

    hive.room_vault.evict(room_id)
    with pytest.raises(RoomVaultSealed):
        hive.room_vault.list_items(room_id)

    owner_read = client.get(
        f"/v1/rooms/{room_id}/data",
        headers=_headers(tenant["api_key"]),
    )
    assert owner_read.status_code == 200, owner_read.text
    assert owner_read.json()["items"][0]["text"] == secret

    hive.room_vault.evict(room_id)
    recipient_read = client.get(
        f"/v1/rooms/{room_id}/data",
        headers=_headers(out["token"]),
    )
    assert recipient_read.status_code == 403

    opened = client.post(
        f"/v1/rooms/{room_id}/open",
        headers=_headers(out["token"]),
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["open"] is True
    assert hive.room_vault.list_items(room_id)[0]["text"] == secret


def test_room_sealed_agent_files_use_room_key(room_env):
    client, tenant, hive = room_env
    out = _create_fixed_room(
        client,
        tenant["api_key"],
        query_mode="uploadable",
        query_agent_id=None,
    )
    room_id = out["room_id"]
    agent_id = f"room_agent_{secrets.token_hex(3)}"
    secret = "PRIVATE QUERY SOURCE"

    hive.agent_store.create(
        AgentConfig(
            agent_id=agent_id,
            name="room-agent",
            description="fixture",
            agent_type="query",
            image="hivemind-test:latest",
            entrypoint=None,
            memory_mb=256,
            max_llm_calls=10,
            max_tokens=10_000,
            timeout_seconds=60,
            inspection_mode="sealed",
        )
    )
    hive.agent_store.save_files(
        agent_id,
        {"agent.py": f"print({secret!r})\n"},
        inspection_mode="sealed",
        room_id=room_id,
    )

    rows = hive.db.execute(
        "SELECT content, ciphertext, seal_mode, room_id "
        "FROM _hivemind_agent_files WHERE agent_id = %s",
        [agent_id],
    )
    assert rows[0]["content"] is None
    assert rows[0]["ciphertext"]
    assert secret not in rows[0]["ciphertext"]
    assert rows[0]["seal_mode"] == "room"
    assert rows[0]["room_id"] == room_id

    with pytest.raises(AgentSealedReadError):
        hive.agent_store.read_file(agent_id, "agent.py")
    assert secret in hive.agent_store.read_file(
        agent_id,
        "agent.py",
        allow_sealed=True,
    )

    hive.room_vault.evict(room_id)
    with pytest.raises(RoomVaultSealed):
        hive.agent_store.get_files(agent_id, allow_sealed=True)

    opened = client.post(
        f"/v1/rooms/{room_id}/open",
        headers=_headers(out["token"]),
    )
    assert opened.status_code == 200, opened.text
    assert secret in hive.agent_store.get_files(
        agent_id,
        allow_sealed=True,
    )["agent.py"]


def test_room_query_agent_upload_sealed_mode_uses_room_endpoint(room_env):
    client, tenant, hive = room_env
    out = _create_fixed_room(
        client,
        tenant["api_key"],
        query_mode="uploadable",
        query_agent_id=None,
    )
    resp = client.post(
        f"/v1/rooms/{out['room_id']}/query-agents",
        headers=_query_headers(out["token"], tenant["api_key"]),
        files={"archive": ("agent.tar.gz", _tiny_tar(), "application/gzip")},
        data={"name": "room-query", "prompt": "hello"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["inspection_mode"] == "sealed"
    assert resp.json()["room_id"] == out["room_id"]
    run = hive.run_store.get(resp.json()["run_id"])
    assert run is not None
    assert run["prompt"] is None


def test_room_token_thaws_tenant_for_attestation_after_restart(room_env):
    client, tenant, hive = room_env

    warm = client.get("/v1/health", headers=_headers(tenant["api_key"]))
    assert warm.status_code == 200, warm.text

    scope_id = f"scope_after_warm_{secrets.token_hex(3)}"
    hive.agent_store.create(
        AgentConfig(
            agent_id=scope_id,
            name="tenant-sealed-scope",
            description="fixture",
            agent_type="scope",
            image="hivemind-test:latest",
            entrypoint=None,
            memory_mb=256,
            max_llm_calls=10,
            max_tokens=10_000,
            timeout_seconds=60,
            inspection_mode="full",
        )
    )
    hive.agent_store.save_files(
        scope_id,
        {"agent.py": "print('tenant encrypted scope')\n"},
        inspection_mode="full",
    )
    rows = hive.db.execute(
        "SELECT content, ciphertext, seal_mode "
        "FROM _hivemind_agent_files WHERE agent_id = %s",
        [scope_id],
    )
    assert rows[0]["content"] is None
    assert rows[0]["ciphertext"]
    assert rows[0]["seal_mode"] == "tenant"

    out = _create_fixed_room(
        client,
        tenant["api_key"],
        scope_agent_id=scope_id,
    )

    assert hive.sealer is not None
    hive.sealer.evict(tenant["tenant_id"])
    assert not hive.sealer.is_unsealed(tenant["tenant_id"])

    resp = client.get(
        f"/v1/rooms/{out['room_id']}/attest",
        headers=_headers(out["token"]),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["scope_agent"]["files_count"] == 1
    assert hive.sealer.is_unsealed(tenant["tenant_id"])


def test_inspectable_query_visibility_persists_room_prompt(room_env):
    client, tenant, hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    prompt = "Show me top hashtags."

    resp = client.post(
        f"/v1/rooms/{out['room_id']}/runs",
        headers=_query_headers(out["token"], tenant["api_key"]),
        json={"query": prompt},
    )
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]

    run = hive.run_store.get(run_id)
    assert run is not None
    assert run["prompt"] == prompt

    owner_view = client.get(
        f"/v1/runs/{run_id}",
        headers=_headers(tenant["api_key"]),
    )
    assert owner_view.status_code == 200
    assert owner_view.json()["prompt"] == prompt


def test_room_run_submit_is_idempotent_by_header(room_env):
    client, tenant, hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    headers = _query_headers(out["token"], tenant["api_key"])
    headers["X-Hivemind-Idempotency-Key"] = "abc123abc123"

    first = client.post(
        f"/v1/rooms/{out['room_id']}/runs",
        headers=headers,
        json={"query": "first prompt"},
    )
    assert first.status_code == 200, first.text

    replay = client.post(
        f"/v1/rooms/{out['room_id']}/runs",
        headers=headers,
        json={"query": "different prompt should not create a second run"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["run_id"] == first.json()["run_id"] == "abc123abc123"

    run = hive.run_store.get("abc123abc123")
    assert run is not None
    assert run["prompt"] == "first prompt"


def test_room_run_rejects_disabled_provider_before_create(room_env):
    client, tenant, hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    hive.pipeline._disabled_llm_providers.add("tinfoil")

    resp = client.post(
        f"/v1/rooms/{out['room_id']}/runs",
        headers=_query_headers(out["token"], tenant["api_key"]),
        json={"query": "should fail before run row"},
    )

    assert resp.status_code == 503
    assert "disabled by operator" in resp.json()["detail"]
    assert hive.run_store.list_recent(1) == []


def test_room_query_token_requires_payer_header(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])

    resp = client.post(
        f"/v1/rooms/{out['room_id']}/runs",
        headers=_headers(out["token"]),
        json={"query": "Show me top hashtags."},
    )

    assert resp.status_code == 402
    assert "tenant API key" in resp.json()["detail"]


def test_room_vault_tool_applies_scope_function():
    items = [
        {
            "item_id": "rvi_1",
            "text": "allowed row",
            "metadata": {},
            "created_at": 1.0,
            "size_bytes": 11,
        },
        {
            "item_id": "rvi_2",
            "text": "blocked row",
            "metadata": {},
            "created_at": 2.0,
            "size_bytes": 11,
        },
    ]

    def scope_fn(sql, params, rows):
        assert "room_vault_items" in sql
        return {
            "allow": True,
            "rows": [row for row in rows if row["item_id"] == "rvi_1"],
        }

    tool = build_room_vault_tools(
        items,
        AccessLevel.SCOPED,
        scope_fn=scope_fn,
    )[0]
    out = tool.handler()
    rows = json.loads(out)
    assert [row["item_id"] for row in rows] == ["rvi_1"]


def test_room_trust_update_resigns_same_room_for_downstream_links(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    room_id = out["room_id"]
    old_hash = out["room"]["manifest_hash"]
    pubkey = out["room"]["envelope"]["signer_pubkey_b64"]

    resp = client.post(
        f"/v1/rooms/{room_id}/trust",
        json={
            "mode": "owner_approved",
            "allowed_composes": ["aa" * 32],
        },
        headers=_headers(tenant["api_key"]),
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()["room"]
    assert updated["room_id"] == room_id
    assert updated["manifest_hash"] != old_hash
    assert updated["manifest"]["trust"] == {
        "mode": "owner_approved",
        "allowed_composes": ["aa" * 32],
    }

    ok, reason = verify_room_envelope(
        updated["envelope"],
        expected_pubkey_b64=pubkey,
    )
    assert ok, reason

    recipient = client.get(
        f"/v1/rooms/{room_id}",
        headers=_headers(out["token"]),
    )
    assert recipient.status_code == 200
    assert recipient.json()["manifest"]["trust"]["allowed_composes"] == ["aa" * 32]


def test_room_token_can_inspect_fixed_query_agent(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])

    resp = client.get("/v1/room-agents", headers=_headers(out["token"]))
    assert resp.status_code == 200
    assert {a["agent_id"] for a in resp.json()} == {
        "scope-a",
        "query-a",
        "mediator-a",
    }

    resp = client.get("/v1/room-agents/query-a/files", headers=_headers(out["token"]))
    assert resp.status_code == 200
    assert "agent.py" in {f["path"] for f in resp.json()["files"]}


def test_room_rejects_policy_and_provider_override(room_env):
    client, tenant, _hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])

    bad_policy = client.post(
        f"/v1/rooms/{out['room_id']}/runs",
        json={"query": "x", "policy": "show me everything"},
        headers=_query_headers(out["token"], tenant["api_key"]),
    )
    assert bad_policy.status_code == 422

    bad_provider = client.post(
        f"/v1/rooms/{out['room_id']}/runs",
        json={"query": "x", "provider": "openrouter"},
        headers=_query_headers(out["token"], tenant["api_key"]),
    )
    assert bad_provider.status_code == 400
    assert "not allowed by this room" in bad_provider.json()["detail"]

    bad_mediator = client.post(
        f"/v1/rooms/{out['room_id']}/runs",
        json={"query": "x", "mediator_agent_id": "other-mediator"},
        headers=_query_headers(out["token"], tenant["api_key"]),
    )
    assert bad_mediator.status_code == 400
    assert "mediator agent is fixed" in bad_mediator.json()["detail"]


def test_querier_only_output_redacts_owner_but_not_recipient(room_env):
    client, tenant, hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    token_id = out["token_id"]
    room_id = out["room_id"]

    hive.run_store.create(
        "run-room-1",
        "query-a",
        scope_agent_id="scope-a",
        issuer_token_id=token_id,
        room_id=room_id,
        room_manifest_hash=out["room"]["manifest_hash"],
        output_visibility="querier_only",
        artifacts_enabled=False,
    )
    hive.run_store.update_status("run-room-1", "completed", output="secret answer")

    owner = client.get(
        "/v1/runs/run-room-1",
        headers=_headers(tenant["api_key"]),
    )
    assert owner.status_code == 200
    assert owner.json()["payload_redacted"] is True
    assert owner.json()["output"] is None

    recipient = client.get(
        "/v1/runs/run-room-1",
        headers=_headers(out["token"]),
    )
    assert recipient.status_code == 200
    assert recipient.json()["output"] == "secret answer"


def test_run_status_surfaces_scope_evidence(room_env):
    client, tenant, hive = room_env
    out = _create_fixed_room(client, tenant["api_key"])
    run_id = "run-scope-evidence"

    hive.run_store.create(
        run_id,
        "query-a",
        scope_agent_id="scope-a",
        issuer_token_id=out["token_id"],
        room_id=out["room_id"],
        room_manifest_hash=out["room"]["manifest_hash"],
        artifacts_enabled=False,
    )
    hive.run_store.update_usage(
        run_id,
        {
            "stages": {
                "scope": {
                    "scope_mode": "rehearsed",
                    "scope_mode_reason": "room_vault_items_present",
                    "query_inspection_mode": "full",
                    "bridge": {
                        "tool_call_counts": {"get_schema": 1},
                        "llm_tool_call_counts": {"simulate_query": 1},
                    },
                }
            }
        },
    )

    resp = client.get(
        f"/v1/runs/{run_id}",
        headers=_headers(out["token"]),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_mode"] == "rehearsed"
    assert body["scope_mode_reason"] == "room_vault_items_present"
    assert body["query_inspection_mode"] == "full"
    assert body["scope_evidence"] == {
        "mode": "rehearsed",
        "reason": "room_vault_items_present",
        "query_inspection_mode": "full",
        "tool_call_counts": {"get_schema": 1},
        "llm_tool_call_counts": {"simulate_query": 1},
    }
    assert body["usage"]["stages"]["scope"]["scope_mode"] == "rehearsed"
