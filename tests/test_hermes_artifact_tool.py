import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "agents/base-hermes/plugins/hivemind/hivemind_tools.py"


def _load_plugin(monkeypatch, role: str):
    registrations: dict[str, dict] = {}

    class FakeRegistry:
        def register(self, *, name, schema, handler, **kwargs):
            registrations[name] = {
                "schema": schema,
                "handler": handler,
                "kwargs": kwargs,
            }

    fake_tools = types.ModuleType("tools")
    fake_registry_mod = types.ModuleType("tools.registry")
    fake_registry_mod.registry = FakeRegistry()
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.setitem(sys.modules, "tools.registry", fake_registry_mod)
    monkeypatch.setenv("HIVEMIND_AGENT_ROLE", role)
    monkeypatch.setenv("BRIDGE_URL", "http://bridge.invalid")
    monkeypatch.setenv("SESSION_TOKEN", "test-token")

    module_name = f"hermes_hivemind_tools_{role}_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, registrations


def test_query_role_registers_artifact_upload_tool(monkeypatch):
    _module, registrations = _load_plugin(monkeypatch, "query")

    assert set(registrations) == {
        "execute_sql",
        "get_schema",
        "upload_artifact",
        "upload_report_artifact",
    }
    schema = registrations["upload_artifact"]["schema"]
    assert schema["name"] == "upload_artifact"
    assert "application/pdf" in schema["parameters"]["properties"]["content_type"]["description"]
    report_schema = registrations["upload_report_artifact"]["schema"]
    assert report_schema["name"] == "upload_report_artifact"
    assert "rendered PDF" in report_schema["description"]


@pytest.mark.asyncio
async def test_upload_artifact_tool_uploads_text(monkeypatch):
    module, registrations = _load_plugin(monkeypatch, "query")
    captured = {}

    async def fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {
            "path": "/v1/runs/run-1/artifacts/report.md",
            "size_bytes": 8,
            "retention_seconds": 86400,
        }

    module._post = fake_post
    result = await registrations["upload_artifact"]["handler"](
        {
            "filename": "report.md",
            "content": "# Report",
            "content_type": "text/markdown",
        }
    )

    assert captured["path"] == "/sandbox/artifact-upload"
    payload = captured["payload"]
    assert payload["filename"] == "report.md"
    assert payload["content_type"] == "text/markdown"
    assert base64.b64decode(payload["content_base64"]).decode() == "# Report"
    assert json.loads(result)["path"] == "/v1/runs/run-1/artifacts/report.md"


@pytest.mark.asyncio
async def test_upload_artifact_tool_accepts_base64_pdf(monkeypatch):
    module, registrations = _load_plugin(monkeypatch, "query")
    captured = {}
    pdf_b64 = base64.b64encode(b"%PDF-1.4\n").decode("ascii")

    async def fake_post(path, payload):
        captured["payload"] = payload
        return {
            "path": "/v1/runs/run-1/artifacts/report.pdf",
            "size_bytes": 9,
            "retention_seconds": 86400,
        }

    module._post = fake_post
    result = await registrations["upload_artifact"]["handler"](
        {
            "filename": "report.pdf",
            "content": pdf_b64,
            "content_type": "application/pdf",
            "encoding": "base64",
        }
    )

    assert captured["payload"]["content_base64"] == pdf_b64
    assert captured["payload"]["content_type"] == "application/pdf"
    assert json.loads(result)["path"].endswith("/report.pdf")


@pytest.mark.asyncio
async def test_upload_report_artifact_tool_posts_markdown(monkeypatch):
    module, registrations = _load_plugin(monkeypatch, "query")
    captured = {}

    async def fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {
            "artifacts": [
                {
                    "path": "/v1/runs/run-1/artifacts/report.md",
                    "size_bytes": 8,
                    "retention_seconds": 86400,
                },
                {
                    "path": "/v1/runs/run-1/artifacts/report.pdf",
                    "size_bytes": 100,
                    "retention_seconds": 86400,
                },
            ]
        }

    module._post = fake_post
    result = await registrations["upload_report_artifact"]["handler"](
        {
            "filename": "report",
            "markdown": "# Report",
            "include_pdf": True,
        }
    )

    assert captured["path"] == "/sandbox/report-artifact"
    assert captured["payload"] == {
        "filename": "report",
        "markdown": "# Report",
        "include_pdf": True,
    }
    assert json.loads(result)["artifacts"][1]["path"].endswith("/report.pdf")
