from __future__ import annotations

import httpx
import pytest

import hivemind.cli._shared as shared


def _response(status: int, *, json: dict | None = None, text: str = ""):
    request = httpx.Request("POST", "https://hivemind.test/v1/rooms/r/runs")
    if json is not None:
        return httpx.Response(status, json=json, request=request)
    return httpx.Response(status, text=text, request=request)


def test_query_tracked_retries_gateway_504_with_same_idempotency_key(monkeypatch):
    posts: list[dict] = []

    def fake_hpost(_url, *, json, headers, timeout):
        posts.append(dict(headers))
        if len(posts) == 1:
            return _response(504, text="<html>gateway timeout</html>")
        return _response(
            200,
            json={"run_id": headers["X-Hivemind-Idempotency-Key"]},
        )

    def fake_hget(_url, *, headers, timeout):
        return _response(
            200,
            json={"status": "completed", "output": "ok", "artifacts": []},
        )

    emitted = {}

    def fake_emit(_service, data, run_id, **_kwargs):
        emitted["run_id"] = run_id
        emitted["status"] = data["status"]

    monkeypatch.setattr(shared, "_hpost", fake_hpost)
    monkeypatch.setattr(shared, "_hget", fake_hget)
    monkeypatch.setattr(shared, "_emit_run_result", fake_emit)
    monkeypatch.setattr(shared.time, "sleep", lambda _seconds: None)

    shared._query_tracked(
        "https://hivemind.test",
        {"Authorization": "Bearer hmq_test"},
        {"query": "x"},
        submit_path="/v1/rooms/r/runs",
    )

    assert len(posts) == 2
    assert posts[0]["X-Hivemind-Idempotency-Key"]
    assert (
        posts[0]["X-Hivemind-Idempotency-Key"]
        == posts[1]["X-Hivemind-Idempotency-Key"]
        == emitted["run_id"]
    )
    assert emitted["status"] == "completed"


def test_query_tracked_does_not_retry_application_503(monkeypatch, capsys):
    posts: list[dict] = []

    def fake_hpost(_url, *, json, headers, timeout):
        posts.append(dict(headers))
        return _response(
            503,
            json={"detail": "LLM provider 'tinfoil' is disabled by operator"},
        )

    monkeypatch.setattr(shared, "_hpost", fake_hpost)
    monkeypatch.setattr(shared.time, "sleep", lambda _seconds: None)

    with pytest.raises(SystemExit) as excinfo:
        shared._query_tracked(
            "https://hivemind.test",
            {"Authorization": "Bearer hmq_test"},
            {"query": "x"},
            submit_path="/v1/rooms/r/runs",
        )

    assert excinfo.value.code == 1
    assert len(posts) == 1
    assert "disabled by operator" in capsys.readouterr().err
