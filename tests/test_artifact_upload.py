"""Tests for the Postgres-backed artifact upload path.

Covers:
  - Bridge /sandbox/artifact-upload endpoint (auth, base64 validation,
    binary round-trip, path shape)
  - ArtifactStore CRUD + TTL sweep semantics
  - RunStore.scrub_expired nulling out expired output payloads
"""
from __future__ import annotations

import base64
import time
from unittest.mock import MagicMock

import pytest
import httpx

from hivemind.sandbox.artifact_store import ArtifactStore
from hivemind.sandbox.bridge import BridgeServer
from hivemind.sandbox.budget import Budget
from hivemind.sandbox.models import validate_artifact_filename
from hivemind.tools import Tool


# ── Shared helpers ──


def _make_tools():
    return [
        Tool(
            name="get_schema",
            description="Get schema",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: "[]",
        ),
    ]


async def _mock_llm_caller(messages, max_tokens, **kwargs):
    return {
        "content": "LLM response",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "finish_reason": "stop",
    }


async def _mock_on_tool_call(name, args):
    return "[]"


def _fake_artifact_store(records: dict) -> MagicMock:
    """MagicMock that mimics ArtifactStore.put() by writing to a dict."""
    store = MagicMock()

    def put(run_id, filename, content, content_type="application/octet-stream"):
        records[(run_id, filename)] = {
            "content": content,
            "content_type": content_type,
            "size_bytes": len(content),
            "created_at": time.time(),
        }
        return {
            "run_id": run_id,
            "filename": filename,
            "size_bytes": len(content),
            "created_at": records[(run_id, filename)]["created_at"],
        }

    store.put = MagicMock(side_effect=put)
    return store


def _bridge_with_artifacts(run_id="test-run-123", store=None):
    return BridgeServer(
        session_token="test-token",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=Budget(max_calls=10, max_tokens=100_000),
        host="127.0.0.1",
        artifact_store=store or _fake_artifact_store({}),
        artifact_retention_seconds=86400,
        run_id=run_id,
        run_store=MagicMock(),
    )


# ═══════════════════════════════════════════════════════════════════════
# 1. Bridge endpoint tests
# ═══════════════════════════════════════════════════════════════════════


class TestBridgeArtifactUpload:

    @pytest.mark.asyncio
    async def test_upload_writes_to_store_and_returns_path(self):
        records: dict = {}
        store = _fake_artifact_store(records)
        server = _bridge_with_artifacts(store=store)
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            payload = b"hello world report data"
            resp = await client.post(
                "/sandbox/artifact-upload",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "filename": "report.json",
                    "content_base64": base64.b64encode(payload).decode(),
                    "content_type": "application/json",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["path"] == "/v1/runs/test-run-123/artifacts/report.json"
            assert data["size_bytes"] == len(payload)
            assert data["retention_seconds"] == 86400

            assert ("test-run-123", "report.json") in records
            rec = records[("test-run-123", "report.json")]
            assert rec["content"] == payload
            assert rec["content_type"] == "application/json"
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_auth_required(self):
        server = _bridge_with_artifacts()
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            resp = await client.post(
                "/sandbox/artifact-upload",
                json={
                    "filename": "test.txt",
                    "content_base64": base64.b64encode(b"data").decode(),
                },
            )
            assert resp.status_code == 401

            resp = await client.post(
                "/sandbox/artifact-upload",
                headers={"Authorization": "Bearer wrong-token"},
                json={
                    "filename": "test.txt",
                    "content_base64": base64.b64encode(b"data").decode(),
                },
            )
            assert resp.status_code == 401
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_invalid_base64_returns_400(self):
        server = _bridge_with_artifacts()
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            resp = await client.post(
                "/sandbox/artifact-upload",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "filename": "bad.bin",
                    "content_base64": "!!!not-valid-base64!!!",
                },
            )
            assert resp.status_code == 400
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_invalid_filename_is_rejected_before_store_write(self):
        records: dict = {}
        store = _fake_artifact_store(records)
        server = _bridge_with_artifacts(store=store)
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            resp = await client.post(
                "/sandbox/artifact-upload",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "filename": "../report.json",
                    "content_base64": base64.b64encode(b"data").decode(),
                },
            )
            assert resp.status_code == 422
            store.put.assert_not_called()
            assert records == {}
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_oversized_artifact_is_rejected_before_store_write(self, monkeypatch):
        monkeypatch.setattr("hivemind.sandbox.bridge.MAX_ARTIFACT_BYTES", 4)
        records: dict = {}
        store = _fake_artifact_store(records)
        server = _bridge_with_artifacts(store=store)
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            resp = await client.post(
                "/sandbox/artifact-upload",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "filename": "too-large.bin",
                    "content_base64": base64.b64encode(b"12345").decode(),
                },
            )
            assert resp.status_code == 413
            store.put.assert_not_called()
            assert records == {}
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_endpoint_not_registered_without_store(self):
        """Bridge without artifact_store should NOT expose the endpoint."""
        server = BridgeServer(
            session_token="tok",
            tools=_make_tools(),
            on_tool_call=_mock_on_tool_call,
            llm_caller=_mock_llm_caller,
            budget=Budget(max_calls=10, max_tokens=100_000),
            host="127.0.0.1",
        )
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            resp = await client.post(
                "/sandbox/artifact-upload",
                headers={"Authorization": "Bearer tok"},
                json={
                    "filename": "test.txt",
                    "content_base64": base64.b64encode(b"data").decode(),
                },
            )
            assert resp.status_code in (404, 405)
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_binary_content_round_trips(self):
        records: dict = {}
        store = _fake_artifact_store(records)
        server = _bridge_with_artifacts(store=store)
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            binary_data = bytes(range(256)) * 10  # all byte values
            resp = await client.post(
                "/sandbox/artifact-upload",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "filename": "image.png",
                    "content_base64": base64.b64encode(binary_data).decode(),
                    "content_type": "image/png",
                },
            )
            assert resp.status_code == 200
            rec = records[("test-run-123", "image.png")]
            assert rec["content"] == binary_data
            assert rec["content_type"] == "image/png"
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_report_artifact_writes_markdown_and_pdf(self):
        records: dict = {}
        store = _fake_artifact_store(records)
        server = _bridge_with_artifacts(store=store)
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            resp = await client.post(
                "/sandbox/report-artifact",
                headers={"Authorization": "Bearer test-token"},
                json={
                    "filename": "watch_report",
                    "markdown": "# Title\n\nA concise report body.",
                    "include_pdf": True,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            paths = [item["path"] for item in data["artifacts"]]
            assert paths == [
                "/v1/runs/test-run-123/artifacts/watch_report.md",
                "/v1/runs/test-run-123/artifacts/watch_report.pdf",
            ]

            md = records[("test-run-123", "watch_report.md")]
            pdf = records[("test-run-123", "watch_report.pdf")]
            assert md["content"] == b"# Title\n\nA concise report body."
            assert md["content_type"] == "text/markdown; charset=utf-8"
            assert pdf["content"].startswith(b"%PDF-1.4")
            assert pdf["content_type"] == "application/pdf"
        finally:
            await client.aclose()
            await server.stop()


# ═══════════════════════════════════════════════════════════════════════
# 2. ArtifactStore unit tests (pure in-memory MagicMock DB)
# ═══════════════════════════════════════════════════════════════════════


class _FakeDB:
    """Minimal DB shim that tracks calls to execute/execute_commit."""

    def __init__(self):
        self.rows: list[dict] = []  # each row = column dict
        self.commit_calls: list[tuple] = []
        self.select_calls: list[tuple] = []

    def execute_commit(self, sql, params):
        self.commit_calls.append((sql, list(params)))
        sql_upper = sql.upper()
        if sql_upper.startswith("INSERT"):
            run_id, filename, content, ctype, size, created = params
            # upsert by (run_id, filename)
            self.rows = [
                r for r in self.rows
                if not (r["run_id"] == run_id and r["filename"] == filename)
            ]
            self.rows.append({
                "run_id": run_id, "filename": filename,
                "content": content, "content_type": ctype,
                "size_bytes": size, "created_at": created,
            })
            return 1
        if sql_upper.startswith("DELETE") and "created_at" in sql:
            cutoff = params[0]
            before = len(self.rows)
            self.rows = [r for r in self.rows if r["created_at"] >= cutoff]
            return before - len(self.rows)
        if sql_upper.startswith("DELETE") and "run_id" in sql:
            run_id = params[0]
            before = len(self.rows)
            self.rows = [r for r in self.rows if r["run_id"] != run_id]
            return before - len(self.rows)
        return 0

    def execute(self, sql, params=()):
        self.select_calls.append((sql, list(params)))
        if "WHERE run_id = %s AND filename = %s" in sql:
            run_id, filename = params
            return [
                r for r in self.rows
                if r["run_id"] == run_id and r["filename"] == filename
            ]
        if "WHERE run_id = %s" in sql:
            run_id = params[0]
            return [
                {k: r[k] for k in (
                    "filename", "content_type", "size_bytes", "created_at"
                )}
                for r in self.rows if r["run_id"] == run_id
            ]
        return []


class TestArtifactStore:

    def test_artifact_filename_validator_rejects_paths_and_headers(self):
        assert validate_artifact_filename("report-1.json") == "report-1.json"
        for unsafe in ("../report.json", "dir/report.json", "bad\r\nx", ".hidden"):
            with pytest.raises(ValueError):
                validate_artifact_filename(unsafe)

    def test_put_and_get_round_trips(self):
        db = _FakeDB()
        store = ArtifactStore(db)
        store.put("run-1", "a.txt", b"hello", "text/plain")
        got = store.get("run-1", "a.txt")
        assert got is not None
        assert got["content"] == b"hello"
        assert got["content_type"] == "text/plain"
        assert got["size_bytes"] == 5

    def test_put_overwrites_on_conflict(self):
        db = _FakeDB()
        store = ArtifactStore(db)
        store.put("run-1", "a.txt", b"first")
        store.put("run-1", "a.txt", b"second")
        got = store.get("run-1", "a.txt")
        assert got["content"] == b"second"

    def test_list_for_run_returns_metadata_only(self):
        db = _FakeDB()
        store = ArtifactStore(db)
        store.put("run-1", "a.txt", b"aaa")
        store.put("run-1", "b.txt", b"bbbb")
        rows = store.list_for_run("run-1")
        assert len(rows) == 2
        assert {r["filename"] for r in rows} == {"a.txt", "b.txt"}

    def test_delete_expired_respects_ttl(self):
        db = _FakeDB()
        store = ArtifactStore(db)
        store.put("run-1", "old.txt", b"old")
        # Backdate the row
        db.rows[0]["created_at"] = time.time() - 10_000
        store.put("run-1", "fresh.txt", b"fresh")
        deleted = store.delete_expired(ttl_seconds=3600)
        assert deleted == 1
        assert {r["filename"] for r in db.rows} == {"fresh.txt"}

    def test_delete_for_run_cascades(self):
        db = _FakeDB()
        store = ArtifactStore(db)
        store.put("run-1", "a.txt", b"aaa")
        store.put("run-1", "b.txt", b"bbb")
        store.put("run-2", "c.txt", b"ccc")
        deleted = store.delete_for_run("run-1")
        assert deleted == 2
        assert [r["filename"] for r in db.rows] == ["c.txt"]

    def test_get_missing_returns_none(self):
        db = _FakeDB()
        store = ArtifactStore(db)
        assert store.get("nope", "missing.txt") is None
