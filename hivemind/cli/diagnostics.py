"""CLI diagnostics for profile, billing, and room readiness."""

from __future__ import annotations

import json as _json

import click
import httpx

from ._config import _headers, _load_config, _profile_name
from ._http import _api_error
from .rooms import (
    _enforce_room_trust,
    _fetch_verified_room,
    _parse_room_ref,
    _room_manifest_is_accepted,
)
from ..version import APP_VERSION


def _hget(*a, **kw):
    from . import _hget as _f
    return _f(*a, **kw)


def _micro_usd(value) -> str:
    return f"${int(value or 0) / 1_000_000:.6f}"


def _add_check(checks: list[dict], name: str, status: str, detail: str = "") -> None:
    checks.append({"name": name, "status": status, "detail": detail})


def _print_checks(checks: list[dict]) -> None:
    labels = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
    for check in checks:
        detail = check.get("detail") or ""
        suffix = f" - {detail}" if detail else ""
        click.echo(
            f"{labels.get(check['status'], check['status']).ljust(4)} "
            f"{check['name']}{suffix}"
        )


@click.command("doctor")
@click.argument("room", required=False)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON only")
def doctor(room: str | None, as_json: bool):
    """Check the active profile, service auth, billing, and optional room."""
    checks: list[dict] = []
    exit_code = 0
    _add_check(checks, "cli", "ok", f"version {APP_VERSION}")

    try:
        config = _load_config()
    except SystemExit as e:
        _add_check(
            checks,
            "profile",
            "fail",
            f"profile '{_profile_name()}' is not usable",
        )
        if as_json:
            click.echo(_json.dumps({"checks": checks}, indent=2))
        raise SystemExit(e.code or 1)

    service = config["service"]
    headers = _headers(config)
    role = config.get("role") or "tenant"
    _add_check(
        checks,
        "profile",
        "ok",
        f"{_profile_name()} ({role}) -> {service}",
    )
    if "Authorization" not in headers:
        _add_check(checks, "api key", "fail", "profile has no api_key")
        exit_code = 1
    else:
        _add_check(checks, "api key", "ok", "present")

    try:
        resp = _hget(f"{service}/v1/health", headers=headers, timeout=15)
    except httpx.RequestError as e:
        _add_check(checks, "service", "fail", str(e))
        exit_code = 1
    else:
        if resp.status_code == 200:
            data = resp.json()
            service_version = str(data.get("version") or "")
            _add_check(
                checks,
                "service",
                "ok",
                f"version {service_version or '?'}, "
                f"tables {data.get('table_count', '?')}",
            )
            if service_version and service_version != APP_VERSION:
                _add_check(
                    checks,
                    "version sync",
                    "warn",
                    f"cli {APP_VERSION}, service {service_version}",
                )
        elif resp.status_code == 401 and role == "admin":
            admin_resp = _hget(
                f"{service}/v1/admin/tenants",
                headers=headers,
                timeout=15,
            )
            if admin_resp.status_code < 400:
                _add_check(checks, "service", "ok", "admin key accepted")
            else:
                _add_check(
                    checks,
                    "service",
                    "fail",
                    f"{admin_resp.status_code}: {_api_error(admin_resp)}",
                )
                exit_code = 1
        else:
            _add_check(
                checks,
                "service",
                "fail",
                f"{resp.status_code}: {_api_error(resp)}",
            )
            exit_code = 1

    if headers.get("Authorization") and role != "admin":
        try:
            resp = _hget(
                f"{service}/v1/billing",
                headers=headers,
                params={"limit": 3},
                timeout=15,
            )
        except httpx.RequestError as e:
            _add_check(checks, "billing", "fail", str(e))
            exit_code = 1
        else:
            if resp.status_code < 400:
                data = resp.json()
                balance = int(data.get("balance_micro_usd") or 0)
                status = "ok" if balance > 0 else "warn"
                detail = f"balance {_micro_usd(balance)}"
                if balance <= 0:
                    detail += " (new paid room asks may be blocked)"
                _add_check(checks, "billing", status, detail)
            else:
                _add_check(
                    checks,
                    "billing",
                    "fail",
                    f"{resp.status_code}: {_api_error(resp)}",
                )
                exit_code = 1

    if room:
        try:
            room_service, room_id, room_headers, owner_pubkey = _parse_room_ref(
                room,
                config=config,
            )
            data = _fetch_verified_room(
                room_service,
                room_id,
                room_headers,
                owner_pubkey_b64=owner_pubkey,
            )
            _enforce_room_trust(data)
            accepted = _room_manifest_is_accepted(
                service=room_service,
                room_id=room_id,
                room_data=data,
                owner_pubkey_b64=owner_pubkey,
            )
            room_data = data.get("room") or {}
            if room_data.get("revoked_at") is not None:
                _add_check(checks, "room", "fail", f"{room_id} is revoked")
                exit_code = 1
            else:
                _add_check(checks, "room", "ok", f"{room_id} manifest verified")
            _add_check(
                checks,
                "room acceptance",
                "ok" if accepted else "warn",
                "accepted locally" if accepted else "run `hivemind room accept ROOM`",
            )
        except Exception as e:
            _add_check(checks, "room", "fail", str(e))
            exit_code = 1

    if as_json:
        click.echo(_json.dumps({"checks": checks}, indent=2, default=str))
    else:
        _print_checks(checks)
    raise SystemExit(exit_code)
