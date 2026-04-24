"""CLI-side trust store for the compose-hash binding.

The CLI fetches ``GET /v1/attestation`` on every remote command and
compares ``compose_hash`` against a local cache. A mismatch prompts
the user to approve the new hash before the command runs.

Store format — ``~/.hivemind/trust.json``:

.. code-block:: json

    {
      "version": 1,
      "services": {
        "https://abcd-8100.app.phala.network": {
          "app_id": "051a174f2457a6c474680a5d745372398f97b6ad",
          "approved_compose_hash": "0x1c4f...",
          "approved_at": "2026-04-25T10:30:00Z",
          "first_seen_at": "2026-04-25T10:30:00Z",
          "history": [
            {"hash": "0x0e11...", "approved_at": "2026-04-20T09:00:00Z"}
          ]
        }
      }
    }

Keyed by service URL (not project dir) so two local projects pointing
at the same CVM share one answer — the threat is operator-side
redeploy, which is a property of the CVM, not the project.

Reference: feedling-mcp-v1's iOS ``BaseImageReference`` TOFU model
(``ios/Feedling/Audit/BaseImageReference.swift``).
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

_TRUST_DIR = Path.home() / ".hivemind"
_TRUST_PATH = _TRUST_DIR / "trust.json"
_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_store() -> dict[str, Any]:
    return {"version": _SCHEMA_VERSION, "services": {}}


def load_trust() -> dict[str, Any]:
    """Read ``~/.hivemind/trust.json`` or return an empty store."""
    if not _TRUST_PATH.exists():
        return _default_store()
    try:
        data = json.loads(_TRUST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_store()
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return _default_store()
    data.setdefault("services", {})
    return data


def save_trust(store: dict[str, Any]) -> None:
    _TRUST_DIR.mkdir(parents=True, exist_ok=True)
    _TRUST_PATH.write_text(
        json.dumps(store, indent=2, sort_keys=True), encoding="utf-8"
    )


def _normalize_service(service: str) -> str:
    return service.rstrip("/")


def get_approved(service: str) -> dict[str, Any] | None:
    """Return the trust entry for a service URL, or None if unknown."""
    store = load_trust()
    return store["services"].get(_normalize_service(service))


def record_approval(
    service: str,
    compose_hash: str,
    app_id: str | None = None,
) -> None:
    """Approve ``compose_hash`` for ``service``, rotating the prior entry
    into ``history``.

    Idempotent: re-approving the same hash is a no-op (preserves
    ``first_seen_at``).
    """
    store = load_trust()
    key = _normalize_service(service)
    existing = store["services"].get(key)
    now = _now_iso()

    if existing and existing.get("approved_compose_hash") == compose_hash:
        if app_id and not existing.get("app_id"):
            existing["app_id"] = app_id
            save_trust(store)
        return

    history = (existing or {}).get("history", [])
    if existing and existing.get("approved_compose_hash"):
        history.append(
            {
                "hash": existing["approved_compose_hash"],
                "approved_at": existing.get("approved_at", ""),
            }
        )

    first_seen = (existing or {}).get("first_seen_at", now)
    store["services"][key] = {
        "app_id": app_id or (existing or {}).get("app_id", ""),
        "approved_compose_hash": compose_hash,
        "approved_at": now,
        "first_seen_at": first_seen,
        "history": history,
    }
    save_trust(store)


def clear(service: str | None = None) -> int:
    """Drop trust for one service or the whole store. Returns entries removed."""
    store = load_trust()
    if service is None:
        n = len(store["services"])
        store["services"] = {}
        save_trust(store)
        return n
    key = _normalize_service(service)
    if key in store["services"]:
        del store["services"][key]
        save_trust(store)
        return 1
    return 0


class TrustDecision:
    """Return value of :func:`evaluate`."""

    __slots__ = ("status", "current_hash", "approved_hash", "app_id", "reason")

    def __init__(
        self,
        status: str,
        current_hash: str = "",
        approved_hash: str = "",
        app_id: str = "",
        reason: str = "",
    ) -> None:
        self.status = status  # one of: trusted, tofu, changed, degraded
        self.current_hash = current_hash
        self.approved_hash = approved_hash
        self.app_id = app_id
        self.reason = reason


def evaluate(
    service: str,
    bundle: dict[str, Any],
) -> TrustDecision:
    """Classify a freshly-fetched attestation bundle against the trust store.

    - ``trusted``  — hash matches the last-approved hash; proceed silently.
    - ``tofu``     — first time we've seen this service; prompt once.
    - ``changed``  — hash differs from last-approved; prompt to approve.
    - ``degraded`` — bundle.ready is false or missing compose_hash; caller
                     decides whether to warn-and-continue or abort.
    """
    if not bundle.get("ready"):
        return TrustDecision(
            status="degraded",
            reason=bundle.get("reason", "attestation_not_ready"),
        )
    att = bundle.get("attestation") or {}
    current = att.get("compose_hash") or ""
    app_id = att.get("app_id") or ""
    if not current:
        return TrustDecision(
            status="degraded",
            reason="bundle_missing_compose_hash",
        )

    existing = get_approved(service)
    if existing is None:
        return TrustDecision(
            status="tofu",
            current_hash=current,
            app_id=app_id,
        )
    if existing.get("approved_compose_hash") == current:
        return TrustDecision(
            status="trusted",
            current_hash=current,
            approved_hash=current,
            app_id=existing.get("app_id") or app_id,
        )
    return TrustDecision(
        status="changed",
        current_hash=current,
        approved_hash=existing.get("approved_compose_hash", ""),
        app_id=existing.get("app_id") or app_id,
    )
