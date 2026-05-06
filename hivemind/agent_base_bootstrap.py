"""Ensure `hivemind-agent-base:latest` exists in the local Docker daemon.

Agent Dockerfiles use `FROM hivemind-agent-base:latest` as a shared base.
In CVM deployments (Phala / dstack) the daemon starts empty, so the first
agent upload fails with `pull access denied for hivemind-agent-base`.

This module is called once at server startup and again before Dockerfile
builds that depend on a shared base. It first tries to pull the Claude-Code
base from GHCR; if that fails (private package, offline, etc.) it builds the
image locally from an inlined Dockerfile that matches ``agents/base/Dockerfile``.
Hermes uses the bundled ``agents/base-hermes`` context when it is available
because that base includes plugin source files.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_GHCR_IMAGE_DEFAULT = "ghcr.io/teleport-computer/hivemind-agent-base:latest"
_LOCAL_TAG = "hivemind-agent-base:latest"

# Pin both the node CLI and the Python SDK to known-compatible versions.
# Post-2.1.109 claude-code + 0.1.66 claude-agent-sdk crash at session start
# with "Command failed with exit code 1" and empty stderr (observed on a
# fresh Phala CVM 2026-04-25). 2.1.109 is the current npm `stable` dist-tag.
_CLAUDE_CODE_VERSION = "2.1.109"
_CLAUDE_AGENT_SDK_VERSION = "0.1.61"
_AIOHTTP_VERSION = "3.13.5"

# Keep this in sync with agents/base/Dockerfile. The boot-time build is the
# fallback when GHCR pull fails, so the recipe must be self-sufficient.
_INLINE_DOCKERFILE = f"""\
FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3

RUN apt-get update && \\
    apt-get install -y --no-install-recommends curl ca-certificates && \\
    curl -fsSL https://deb.nodesource.com/setup_20.x -o /tmp/nodesource_setup.sh && \\
    bash /tmp/nodesource_setup.sh && \\
    apt-get install -y --no-install-recommends nodejs && \\
    rm -f /tmp/nodesource_setup.sh && \\
    rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@{_CLAUDE_CODE_VERSION}

RUN pip install --no-cache-dir "claude-agent-sdk=={_CLAUDE_AGENT_SDK_VERSION}" "aiohttp=={_AIOHTTP_VERSION}"

RUN useradd -m -s /bin/bash agent

WORKDIR /app
RUN chown agent:agent /app
ENV PYTHONPATH=/app

USER agent
"""

# Short hash of the recipe. Stamped as a label on the built image so
# ensure_agent_base_image() can detect when the recipe changed and rebuild
# instead of reusing a stale cached image.
_RECIPE_HASH = hashlib.sha256(_INLINE_DOCKERFILE.encode()).hexdigest()[:16]
_RECIPE_LABEL = "com.hivemind.agentbase.recipe-hash"


def _client():
    import docker  # deferred — tests and CLI may not have docker
    return docker.from_env()


def _image_present(tag: str) -> bool:
    import docker.errors
    try:
        _client().images.get(tag)
        return True
    except docker.errors.ImageNotFound:
        return False
    except Exception as e:
        logger.warning("agent-base bootstrap: image inspect failed: %s", e)
        return False


def _image_recipe_hash(tag: str) -> str | None:
    """Read the recipe hash label from an existing local image, if any."""
    import docker.errors
    try:
        img = _client().images.get(tag)
        return (img.attrs.get("Config", {}).get("Labels") or {}).get(_RECIPE_LABEL)
    except docker.errors.ImageNotFound:
        return None
    except Exception as e:
        logger.warning("agent-base bootstrap: label inspect failed: %s", e)
        return None


def _pull_and_tag(source: str) -> bool:
    try:
        client = _client()
        logger.info("agent-base bootstrap: pulling %s", source)
        img = client.images.pull(source)
        img.tag(_LOCAL_TAG.split(":")[0], tag=_LOCAL_TAG.split(":")[1])
        logger.info("agent-base bootstrap: tagged %s from %s", _LOCAL_TAG, source)
        return True
    except Exception as e:
        logger.info("agent-base bootstrap: pull failed (%s)", e)
        return False


def _build_inline() -> bool:
    try:
        client = _client()
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "Dockerfile"), "w", encoding="utf-8") as f:
                f.write(_INLINE_DOCKERFILE)
            logger.info(
                "agent-base bootstrap: building %s from inline Dockerfile (recipe=%s)",
                _LOCAL_TAG, _RECIPE_HASH,
            )
            client.images.build(
                path=tmp,
                tag=_LOCAL_TAG,
                rm=True,
                labels={_RECIPE_LABEL: _RECIPE_HASH},
            )
        logger.info("agent-base bootstrap: built %s", _LOCAL_TAG)
        return True
    except Exception as e:
        logger.error("agent-base bootstrap: inline build failed: %s", e)
        return False


def ensure_agent_base_image() -> bool:
    """Guarantee `hivemind-agent-base:latest` is in the daemon.

    Fast path: image already tagged AND stamped with the current recipe hash
    → reuse. If the stored hash differs (or is missing) the image was built
    from an older recipe; we discard and rebuild so pinned versions take
    effect on the next server start.

    Slow path: pull from GHCR, else build from inline Dockerfile. The built
    image is stamped with the recipe hash for the next startup check.

    Returns True on success.
    """
    stored = _image_recipe_hash(_LOCAL_TAG)
    if stored == _RECIPE_HASH:
        logger.info("agent-base bootstrap: %s already present (recipe=%s)",
                    _LOCAL_TAG, _RECIPE_HASH)
        return True
    if stored is not None:
        logger.info(
            "agent-base bootstrap: %s recipe mismatch (have=%s want=%s) — rebuilding",
            _LOCAL_TAG, stored, _RECIPE_HASH,
        )
        try:
            _client().images.remove(_LOCAL_TAG, force=True)
        except Exception as e:
            logger.warning("agent-base bootstrap: remove stale image failed: %s", e)

    source = os.environ.get("HIVEMIND_AGENT_BASE_IMAGE", _GHCR_IMAGE_DEFAULT)
    if _pull_and_tag(source):
        # GHCR image may lack the recipe label (built by CI from agents/base).
        # If that's OK for this deployment, accept it; otherwise inline build
        # will run on next restart when the recipe is updated.
        return True
    return _build_inline()


# ── Hermes base image ──────────────────────────────────────────────────────
#
# The hermes base bundles plugin Python files (agents/base-hermes/plugins/).
# Production core images copy agents/ into /app/agents, so when GHCR is
# private or unreachable we can still build the trusted base locally before
# building role-specific bundled defaults.

_HERMES_GHCR_IMAGE_DEFAULT = (
    "ghcr.io/teleport-computer/hivemind-agent-base-hermes:latest"
)
_HERMES_LOCAL_TAG = "hivemind-agent-base-hermes:latest"
_HERMES_RECIPE_LABEL = "com.hivemind.agentbase-hermes.recipe-hash"


def _image_label(tag: str, label: str) -> str | None:
    """Read one label from an existing local image, if any."""
    import docker.errors
    try:
        img = _client().images.get(tag)
        return (img.attrs.get("Config", {}).get("Labels") or {}).get(label)
    except docker.errors.ImageNotFound:
        return None
    except Exception as e:
        logger.warning("agent-base bootstrap: label inspect failed: %s", e)
        return None


def _candidate_agents_roots() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("HIVEMIND_BUNDLED_AGENTS_DIR", "").strip()
    if configured:
        roots.append(Path(configured))
    roots.append(Path("/app/agents"))
    roots.append(Path(__file__).resolve().parents[1] / "agents")
    return roots


def _bundled_hermes_base_dir() -> Path | None:
    for root in _candidate_agents_roots():
        candidate = root / "base-hermes"
        if (candidate / "Dockerfile").is_file():
            return candidate
    return None


def _hash_build_context(path: Path) -> str:
    h = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        if "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        rel = item.relative_to(path).as_posix()
        h.update(rel.encode())
        h.update(b"\0")
        h.update(item.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def _build_hermes_from_bundled(source_dir: Path, recipe_hash: str) -> bool:
    try:
        client = _client()
        logger.info(
            "agent-base hermes bootstrap: building %s from %s (recipe=%s)",
            _HERMES_LOCAL_TAG, source_dir, recipe_hash,
        )
        client.images.build(
            path=str(source_dir),
            tag=_HERMES_LOCAL_TAG,
            rm=True,
            forcerm=True,
            labels={_HERMES_RECIPE_LABEL: recipe_hash},
        )
        logger.info("agent-base hermes bootstrap: built %s", _HERMES_LOCAL_TAG)
        return True
    except Exception as e:
        logger.error("agent-base hermes bootstrap: bundled build failed: %s", e)
        return False


def ensure_agent_base_hermes_image() -> bool:
    """Guarantee `hivemind-agent-base-hermes:latest` is in the daemon.

    Fast path: reuse a local image stamped with the current bundled recipe.
    Otherwise try GHCR, then build from bundled source when available.
    Returns False only when neither pull nor bundled build succeeds.
    """
    bundled = _bundled_hermes_base_dir()
    recipe_hash = _hash_build_context(bundled) if bundled is not None else None
    if recipe_hash and _image_label(_HERMES_LOCAL_TAG, _HERMES_RECIPE_LABEL) == recipe_hash:
        logger.info(
            "agent-base hermes bootstrap: %s already present (recipe=%s)",
            _HERMES_LOCAL_TAG, recipe_hash,
        )
        return True
    if recipe_hash is None and _image_present(_HERMES_LOCAL_TAG):
        return True

    source = os.environ.get(
        "HIVEMIND_AGENT_BASE_HERMES_IMAGE", _HERMES_GHCR_IMAGE_DEFAULT
    )
    try:
        client = _client()
        logger.info("agent-base bootstrap: pulling %s", source)
        img = client.images.pull(source)
        name, tag = _HERMES_LOCAL_TAG.rsplit(":", 1)
        img.tag(name, tag=tag)
        logger.info(
            "agent-base bootstrap: tagged %s from %s", _HERMES_LOCAL_TAG, source
        )
        return True
    except Exception as e:
        logger.info("agent-base hermes bootstrap: pull failed (%s)", e)
    if bundled is not None and recipe_hash is not None:
        return _build_hermes_from_bundled(bundled, recipe_hash)
    return False
