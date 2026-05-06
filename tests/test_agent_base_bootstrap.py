from unittest.mock import MagicMock

from hivemind import agent_base_bootstrap as bootstrap


def test_ensure_agent_base_hermes_builds_from_bundled_source(monkeypatch, tmp_path):
    agents_root = tmp_path / "agents"
    base_dir = agents_root / "base-hermes"
    plugin_dir = base_dir / "plugins" / "hivemind"
    plugin_dir.mkdir(parents=True)
    (base_dir / "Dockerfile").write_text(
        "FROM python:3.12-slim\nCOPY plugins/ /opt/hivemind/plugins/\n"
    )
    (plugin_dir / "__init__.py").write_text("")
    (plugin_dir / "hivemind_tools.py").write_text("# plugin\n")

    client = MagicMock()
    client.images.pull.side_effect = RuntimeError("private")

    monkeypatch.setenv("HIVEMIND_BUNDLED_AGENTS_DIR", str(agents_root))
    monkeypatch.setattr(bootstrap, "_client", lambda: client)
    monkeypatch.setattr(bootstrap, "_image_label", lambda _tag, _label: None)
    monkeypatch.setattr(bootstrap, "_image_present", lambda _tag: False)

    assert bootstrap.ensure_agent_base_hermes_image() is True

    client.images.pull.assert_called_once()
    client.images.build.assert_called_once()
    _, kwargs = client.images.build.call_args
    assert kwargs["path"] == str(base_dir)
    assert kwargs["tag"] == "hivemind-agent-base-hermes:latest"
    assert bootstrap._HERMES_RECIPE_LABEL in kwargs["labels"]
