"""Profile / config helpers for the CLI.

The path constants ``_HIVEMIND_HOME``, ``_PROFILES_DIR``, and
``_ACTIVE_POINTER`` are owned by the parent ``hivemind.cli`` package
(``__init__.py``). Tests in ``tests/test_cli_trust.py`` rely on
``monkeypatch.setattr(hivemind.cli, "_HIVEMIND_HOME", ...)`` to redirect
the trust store and profiles dir into a sandbox tmp_path. We therefore
look them up via deferred ``from . import ...`` inside each function so
patches on the parent module take effect on the next call.
"""

import os
from pathlib import Path

import click
import yaml

from ._http import _warm_pin_from_trust

_DEFAULT_PROFILE = "default"

# Legacy CWD-scoped config we auto-migrate from (one-shot).
_LEGACY_CONFIG_DIR = ".hivemind"
_LEGACY_CONFIG_FILE = "config.yaml"


def _profile_name() -> str:
    """Active profile name. Resolution order:

    1. ``HIVEMIND_PROFILE`` env var (set by ``--profile NAME``)
    2. The persistent pointer file at ``~/.hivemind/active``
       (written by ``hivemind profile use NAME``)
    3. ``"default"`` — the legacy fallback
    """
    from . import _ACTIVE_POINTER  # parent-owned for test patching

    env = os.environ.get("HIVEMIND_PROFILE", "").strip()
    if env:
        return env
    try:
        if _ACTIVE_POINTER.exists():
            name = _ACTIVE_POINTER.read_text().strip()
            if name:
                return name
    except OSError:
        pass
    return _DEFAULT_PROFILE


def _set_active_profile(name: str) -> None:
    from . import _ACTIVE_POINTER, _HIVEMIND_HOME

    _HIVEMIND_HOME.mkdir(parents=True, exist_ok=True)
    _ACTIVE_POINTER.write_text(name + "\n")


def _clear_active_profile_if(name: str) -> None:
    """Remove the active pointer iff it currently points at ``name``."""
    from . import _ACTIVE_POINTER

    try:
        if (
            _ACTIVE_POINTER.exists()
            and _ACTIVE_POINTER.read_text().strip() == name
        ):
            _ACTIVE_POINTER.unlink()
    except OSError:
        pass


def _config_path(profile: str | None = None) -> Path:
    from . import _PROFILES_DIR

    name = profile or _profile_name()
    return _PROFILES_DIR / f"{name}.yaml"


def _legacy_config_path() -> Path:
    return Path(_LEGACY_CONFIG_DIR) / _LEGACY_CONFIG_FILE


def _maybe_migrate_legacy(target: Path) -> bool:
    """One-shot migration from `./.hivemind/config.yaml` → global default profile."""
    legacy = _legacy_config_path()
    if not legacy.exists() or target.exists():
        return False
    if _profile_name() != _DEFAULT_PROFILE:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(legacy.read_bytes())
    click.echo(
        f"Migrated legacy {legacy} → {target} (default profile). "
        f"You can delete {_LEGACY_CONFIG_DIR}/ when ready.",
        err=True,
    )
    return True


def _load_config(*, check_trust: bool = True) -> dict:
    """Load the active profile's config or exit with error."""
    from . import _PROFILES_DIR

    p = _config_path()
    _maybe_migrate_legacy(p)
    if not p.exists():
        profile = _profile_name()
        existing = (
            sorted(x.stem for x in _PROFILES_DIR.glob("*.yaml"))
            if _PROFILES_DIR.exists()
            else []
        )
        if profile == _DEFAULT_PROFILE and existing:
            hint = (
                f"Profile '{profile}' doesn't exist. Pick one of "
                f"{existing} via 'hivemind profile use NAME', "
                f"or pass --profile NAME on each command."
            )
        elif profile == _DEFAULT_PROFILE:
            hint = f"Run 'hivemind init --api-key …' to create profile '{profile}'."
        else:
            hint = f"Run 'hivemind --profile {profile} init --api-key …' to create it."
        click.echo(
            f"Error: profile '{profile}' not found at {p}. {hint}",
            err=True,
        )
        raise SystemExit(1)
    try:
        with open(p) as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        click.echo(f"Error: Corrupt config file {p}: {e}", err=True)
        raise SystemExit(1)
    if not config.get("service"):
        click.echo(
            f"Error: Config {p} missing 'service' URL. Run 'hivemind init' again.",
            err=True,
        )
        raise SystemExit(1)
    _warm_pin_from_trust(config["service"])
    if check_trust:
        from ._trust import _require_trust
        _require_trust(config)
    return config


def _save_config(config: dict) -> None:
    from . import _ACTIVE_POINTER

    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    if not _ACTIVE_POINTER.exists():
        _set_active_profile(_profile_name())


def _headers(config: dict) -> dict:
    h: dict[str, str] = {}
    if config.get("api_key"):
        h["Authorization"] = f"Bearer {config['api_key']}"
    return h
