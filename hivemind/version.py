from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as package_version


_PACKAGE_NAME = "hivemind-core"
_FALLBACK_VERSION = "0.3.3"


def resolve_version() -> str:
    try:
        return package_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return _FALLBACK_VERSION


APP_VERSION = resolve_version()
