"""Shared helpers for room-bound API routes and runs."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import HTTPException, Request

from ..models import QueryRequest
from ..rooms import (
    RoomTrustUpdateRequest,
    inspection_mode_from_visibility,
    verify_room_envelope,
)
from ..tenants import Caller


async def load_room_for_caller(
    caller: Caller,
    room_id: str | None,
    *,
    allow_revoked: bool = False,
) -> dict:
    """Resolve a room for the caller.

    By default rejects revoked rooms with 403 because operational endpoints
    (data add, runs, etc.) cannot proceed against a revoked room. The
    read-only GET ``/v1/rooms/{id}`` passes ``allow_revoked=True`` so the
    UI can render the manifest of a revoked room (e.g. so the owner can
    see what they revoked, or confirm before purging from the list).
    """
    rid = (room_id or "").strip()
    if caller.role == "query":
        bound = (caller.constraints.get("room_id") or "").strip()
        if not bound:
            raise HTTPException(400, "query token is not bound to a room")
        if rid and rid != bound:
            raise HTTPException(403, "query token is bound to a different room")
        rid = bound
    if caller.role == "share":
        bound = (caller.constraints.get("room_id") or "").strip()
        if not bound:
            raise HTTPException(400, "share-link token is not bound to a room")
        if rid and rid != bound:
            raise HTTPException(
                403, "share-link token is bound to a different room"
            )
        rid = bound
    if not rid:
        raise HTTPException(400, "room_id is required")
    room = await asyncio.to_thread(caller.hive.room_store.get, rid)
    if not room:
        raise HTTPException(404, f"room '{rid}' not found")
    ok, reason = verify_room_envelope(room.get("envelope") or {})
    if not ok:
        raise HTTPException(
            409,
            f"room '{rid}' has an invalid signed manifest: {reason}",
        )
    if not allow_revoked and room.get("revoked_at") is not None:
        raise HTTPException(403, f"room '{rid}' is revoked")
    return room


def validate_room_provider(req_provider: str | None, room: dict) -> None:
    allowed = [p.strip().lower() for p in room.get("allowed_llm_providers") or []]
    requested = (req_provider or "").strip().lower()
    if not allowed:
        if requested:
            raise HTTPException(
                400,
                "this room disallows external LLM egress; omit provider",
            )
        return
    if requested and requested not in allowed:
        raise HTTPException(
            400,
            f"provider '{requested}' is not allowed by this room "
            f"(allowed_llm_providers={allowed})",
        )


def signed_allowed_tables(room: dict) -> list[str]:
    """Return the signed SQL allowlist or reject obsolete manifests."""
    manifest = room.get("manifest") or {}
    if "allowed_tables" not in manifest or manifest.get("allowed_tables") is None:
        raise HTTPException(
            410,
            "room manifest is missing signed allowed_tables; recreate the room",
        )
    raw = manifest.get("allowed_tables")
    if not isinstance(raw, list):
        raise HTTPException(409, "room manifest has invalid allowed_tables")
    return [str(t).strip() for t in raw if str(t).strip()]


def room_query_inspection_mode(room: dict) -> str:
    manifest = room.get("manifest") or {}
    query = manifest.get("query") or {}
    return inspection_mode_from_visibility(query.get("visibility"))


def room_prompt_for_run(room: dict | None, prompt: str) -> str | None:
    if not room:
        return None
    manifest = room.get("manifest") or {}
    query = manifest.get("query") or {}
    if query.get("visibility") != "inspectable":
        return None
    return prompt


def room_wrap_id(caller: Caller) -> str:
    if caller.role == "owner":
        return "owner"
    token_id = (caller.token_id or "").strip()
    if not token_id:
        raise HTTPException(500, f"{caller.role} caller is missing token_id")
    if caller.role == "share":
        return f"share:{token_id}"
    return f"query:{token_id}"


def _first_header_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def _parse_forwarded_header(value: str | None) -> dict[str, str]:
    """Parse the first RFC 7239 Forwarded hop into a small key/value map."""
    first = _first_header_value(value)
    if not first:
        return {}
    out: dict[str, str] = {}
    for part in first.split(";"):
        key, sep, raw = part.strip().partition("=")
        if not sep:
            continue
        cleaned = raw.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] == '"':
            cleaned = cleaned[1:-1]
        out[key.strip().lower()] = cleaned
    return out


def _with_forwarded_port(host: str, proto: str, port: str) -> str:
    port = port.strip()
    if not port.isdigit():
        return host
    if (proto, port) in {("http", "80"), ("https", "443")}:
        return host
    # Host may be "example.com:8443" or "[::1]:8443" already.
    suffix = host.rsplit("]", 1)[-1] if host.startswith("[") else host
    if ":" in suffix:
        return host
    return f"{host}:{port}"


def external_request_base(request: Request) -> tuple[str, str]:
    """Return the externally visible ``(base_url, host)`` for room links.

    In production the app sits behind dstack-ingress: FastAPI sees HTTP on
    the Docker bridge, while clients must use the public HTTPS origin for
    strict attestation. Prefer proxy headers when present; otherwise keep
    TestClient/local behavior unchanged.
    """
    forwarded = _parse_forwarded_header(request.headers.get("forwarded"))
    proto = (
        _first_header_value(request.headers.get("x-forwarded-proto"))
        or forwarded.get("proto")
        or request.url.scheme
        or "http"
    ).lower()
    if proto not in {"http", "https"}:
        proto = request.url.scheme or "http"

    host = (
        _first_header_value(request.headers.get("x-forwarded-host"))
        or forwarded.get("host")
        or request.url.netloc
        or "service"
    )
    port = _first_header_value(request.headers.get("x-forwarded-port"))
    if port:
        host = _with_forwarded_port(host, proto, port)

    return f"{proto}://{host}".rstrip("/"), host


def room_link(request: Request, room_id: str, token: str, pubkey_b64: str) -> str:
    base, host = external_request_base(request)
    return (
        f"hmroom://{host}/{room_id}"
        f"?service={quote(base, safe='')}"
        f"&token={quote(token, safe='')}"
        f"&owner_pubkey={quote(pubkey_b64, safe='')}"
    )


def share_room_link(
    request: Request,
    room_id: str,
    share_token: str,
    pubkey_b64: str,
) -> str:
    """Stable share-link URI. Same shape as :func:`room_link` but the
    capability is carried in ``?share=`` so the server can route the
    bearer through the share-link auth path instead of invite-token."""
    base, host = external_request_base(request)
    return (
        f"hmroom://{host}/{room_id}"
        f"?service={quote(base, safe='')}"
        f"&share={quote(share_token, safe='')}"
        f"&owner_pubkey={quote(pubkey_b64, safe='')}"
    )


def apply_room_to_query_request(
    req: QueryRequest,
    room: dict,
) -> QueryRequest:
    manifest = room.get("manifest") or {}
    query = manifest.get("query") or {}
    mediator = manifest.get("mediator")
    mode = query.get("mode") or room.get("query_mode")
    fixed_query_agent_id = (
        query.get("agent_id") or room.get("fixed_query_agent_id") or ""
    ).strip()
    if mode == "fixed":
        if not fixed_query_agent_id:
            raise HTTPException(500, "room fixed query agent is missing")
        query_agent_id = fixed_query_agent_id
    else:
        query_agent_id = (req.query_agent_id or "").strip()
        if not query_agent_id:
            raise HTTPException(
                400,
                "room requires a query_agent_id; upload a query agent "
                "or use a room with query.mode='fixed'",
            )
    room_policy = room.get("policy") or ""
    requested_policy = (req.policy or "").strip()
    if requested_policy and requested_policy != room_policy:
        raise HTTPException(
            400,
            "room policy is fixed by the signed room manifest; "
            "caller-supplied policy cannot override it",
        )
    mediator_agent_id = req.mediator_agent_id
    if isinstance(mediator, dict):
        fixed_mediator_agent_id = (mediator.get("agent_id") or "").strip()
        requested_mediator_agent_id = (req.mediator_agent_id or "").strip()
        if fixed_mediator_agent_id:
            if (
                requested_mediator_agent_id
                and requested_mediator_agent_id != fixed_mediator_agent_id
            ):
                raise HTTPException(
                    400,
                    "room mediator agent is fixed by the signed room "
                    "manifest; caller-supplied mediator cannot override it",
                )
            mediator_agent_id = fixed_mediator_agent_id
        elif requested_mediator_agent_id:
            raise HTTPException(
                400,
                "room manifest does not allow a mediator-agent override",
            )
    validate_room_provider(req.provider, room)
    return req.model_copy(
        update={
            "room_id": room["room_id"],
            "scope_agent_id": room["scope_agent_id"],
            "query_agent_id": query_agent_id,
            "mediator_agent_id": mediator_agent_id,
            "policy": room_policy,
        }
    )


def live_compose_hash() -> str:
    from .. import attestation as _att

    bundle = _att.get_bundle()
    if not bundle.get("ready"):
        return ""
    return ((bundle.get("attestation") or {}).get("compose_hash") or "").lower()


def compose_trust_from_update(
    current: dict,
    req: RoomTrustUpdateRequest,
) -> dict:
    mode = req.mode or current.get("mode") or "operator_updates"
    if req.allowed_composes is None:
        allowed = [
            str(c).strip().lower()
            for c in (current.get("allowed_composes") or [])
            if str(c).strip()
        ]
    else:
        allowed = [
            str(c).strip().lower()
            for c in req.allowed_composes
            if str(c).strip()
        ]
    if req.append_live:
        live = live_compose_hash()
        if not live:
            raise HTTPException(400, "live compose_hash is not available")
        if live not in allowed:
            allowed.append(live)
    if mode in {"pinned", "owner_approved"} and not allowed:
        raise HTTPException(
            400,
            f"trust.mode='{mode}' requires allowed_composes or append_live=true",
        )
    return {"mode": mode, "allowed_composes": allowed}
