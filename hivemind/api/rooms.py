"""Public room lifecycle, vault, trust, attestation, and run routes."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from fastapi import Depends, FastAPI, HTTPException, Request

from .agent_registry import build_agent_attestation
from .room_helpers import (
    apply_room_to_query_request,
    compose_trust_from_update,
    live_compose_hash,
    load_room_for_caller,
    room_link,
    room_wrap_id,
)
from ..config import Settings
from ..models import QueryRequest
from ..rooms import (
    RoomCreateRequest,
    RoomRunRequest,
    RoomTrustUpdateRequest,
    RoomVaultItemRequest,
    build_room_manifest,
    room_constraints,
    sign_manifest,
    visibility_from_inspection_mode,
)
from ..tenant_signing import derive_signing_keypair
from ..tenants import Caller


def register_room_routes(
    app: FastAPI,
    settings: Settings,
    bearer: Callable[[Request], str],
    requires_role: Callable[..., Callable],
    submit_query_run_for_request: Callable,
) -> None:
    """Register public room routes."""

    @app.post("/v1/rooms")
    async def create_room(
        req: RoomCreateRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        """Create a signed room manifest and a recipient invite token."""
        hm = caller.hive
        scope_cfg = await asyncio.to_thread(
            hm.agent_store.get, req.scope_agent_id,
        )
        if not scope_cfg:
            raise HTTPException(404, f"Scope agent '{req.scope_agent_id}' not found")
        actual_scope_visibility = visibility_from_inspection_mode(
            getattr(scope_cfg, "inspection_mode", "full")
        )
        if req.scope_visibility and req.scope_visibility != actual_scope_visibility:
            raise HTTPException(
                400,
                "scope_visibility does not match the registered scope "
                f"agent inspection_mode ({actual_scope_visibility})",
            )
        scope_visibility = actual_scope_visibility

        query_visibility = req.query_visibility
        if req.query_mode == "fixed":
            query_cfg = await asyncio.to_thread(
                hm.agent_store.get, req.query_agent_id,
            )
            if not query_cfg:
                raise HTTPException(
                    404, f"Query agent '{req.query_agent_id}' not found"
                )
            query_visibility = visibility_from_inspection_mode(
                getattr(query_cfg, "inspection_mode", "full")
            )

        if not req.mediator_agent_id:
            req.mediator_agent_id = settings.default_mediator_agent or None
        mediator_visibility = None
        if req.mediator_agent_id:
            mediator_cfg = await asyncio.to_thread(
                hm.agent_store.get, req.mediator_agent_id,
            )
            if not mediator_cfg:
                raise HTTPException(
                    404, f"Mediator agent '{req.mediator_agent_id}' not found"
                )
            mediator_visibility = visibility_from_inspection_mode(
                getattr(mediator_cfg, "inspection_mode", "full")
            )
            if (
                req.mediator_visibility
                and req.mediator_visibility != mediator_visibility
            ):
                raise HTTPException(
                    400,
                    "mediator_visibility does not match the registered mediator "
                    f"agent inspection_mode ({mediator_visibility})",
                )

        # Validate per-room data allowlist against the live tenant DB schema.
        # The tools layer enforces the allowlist at runtime; this check just
        # catches typos and stale table names early, before the manifest is
        # signed.
        if req.allowed_tables is not None and req.allowed_tables:
            requested = {t.lower() for t in req.allowed_tables}
            try:
                rows = await asyncio.to_thread(
                    hm.db.execute,
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' "
                    "AND table_name NOT LIKE %s",
                    ["\\_%"],
                )
            except Exception:
                rows = []
            existing = {str(r.get("table_name", "")).lower() for r in rows}
            missing = requested - existing
            if missing:
                raise HTTPException(
                    400,
                    "allowed_tables references tables that don't exist in "
                    "your tenant DB: "
                    + ", ".join(sorted(missing))
                    + ". Create them first via /v1/tenant/sql, or "
                    "remove them from the allowed_tables list.",
                )

        if (
            req.trust.mode in {"pinned", "owner_approved"}
            and not req.trust.allowed_composes
        ):
            compose_hash = live_compose_hash()
            if not compose_hash:
                raise HTTPException(
                    400,
                    "strict room trust mode requires a live compose_hash; "
                    "pass trust.allowed_composes explicitly or use "
                    "trust.mode='operator_updates'",
                )
            req.trust.allowed_composes = [compose_hash]

        owner_bearer = bearer(request)
        priv, pub = derive_signing_keypair(owner_bearer, caller.tenant_id)
        pub_b64 = base64.b64encode(
            pub.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode("ascii")

        room_id = f"room_{uuid4().hex[:12]}"
        manifest = build_room_manifest(
            room_id=room_id,
            tenant_id=caller.tenant_id,
            created_at=time.time(),
            req=req,
            scope_visibility=scope_visibility,
            query_visibility=query_visibility,
            mediator_visibility=mediator_visibility,
            signer_pubkey_b64=pub_b64,
        )
        envelope = sign_manifest(manifest, priv)
        room = await asyncio.to_thread(hm.room_store.create, envelope)

        registry = request.app.state.registry
        try:
            cap = await asyncio.to_thread(
                registry.mint_capability,
                caller.tenant_id,
                "query",
                f"room:{req.name or room_id}",
                room_constraints(envelope),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))

        dek = await asyncio.to_thread(
            hm.room_vault.ensure_room_key,
            room_id,
            "owner",
            owner_bearer,
        )
        await asyncio.to_thread(
            hm.room_vault.add_wrap,
            room_id,
            f"query:{cap['token_id']}",
            cap["token"],
            dek=dek,
        )

        return {
            "room_id": room_id,
            "room": room,
            "token": cap["token"],
            "token_id": cap["token_id"],
            "link": room_link(request, room_id, cap["token"], pub_b64),
        }

    @app.get("/v1/rooms")
    async def list_rooms(
        limit: int = 50,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        if caller.role == "query":
            room = await load_room_for_caller(
                caller, caller.constraints.get("room_id")
            )
            return {"rooms": [room]}
        rooms = await asyncio.to_thread(caller.hive.room_store.list, limit)
        return {"rooms": rooms}

    @app.get("/v1/rooms/{room_id}")
    async def get_room(
        room_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        # GET is read-only — return revoked rooms with the revoked_at
        # flag set so UIs can render "this room was revoked" instead of
        # showing a 403 dead end.
        room = await load_room_for_caller(
            caller, room_id, allow_revoked=True,
        )
        return room

    @app.get("/v1/rooms/{room_id}/attest")
    async def attest_room(
        room_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        from .. import attestation as _att

        room = await load_room_for_caller(caller, room_id)
        scope = await build_agent_attestation(caller, room["scope_agent_id"])
        fixed_query = None
        if room.get("fixed_query_agent_id"):
            fixed_query = await build_agent_attestation(
                caller, room["fixed_query_agent_id"]
            )
        fixed_mediator = None
        if room.get("fixed_mediator_agent_id"):
            fixed_mediator = await build_agent_attestation(
                caller, room["fixed_mediator_agent_id"]
            )
        return {
            "room": room,
            "scope_agent": scope,
            "query_agent": fixed_query,
            "mediator_agent": fixed_mediator,
            "attestation": _att.get_bundle(),
        }

    @app.get("/v1/rooms/{room_id}/key")
    async def room_vault_status(
        room_id: str,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        room = await load_room_for_caller(caller, room_id)
        return await asyncio.to_thread(caller.hive.room_vault.status, room["room_id"])

    @app.post("/v1/rooms/{room_id}/open")
    async def open_room_vault(
        room_id: str,
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        room = await load_room_for_caller(caller, room_id)
        await asyncio.to_thread(
            caller.hive.room_vault.open,
            room["room_id"],
            room_wrap_id(caller),
            bearer(request),
        )
        return await asyncio.to_thread(caller.hive.room_vault.status, room["room_id"])

    @app.post("/v1/rooms/{room_id}/data")
    async def add_room_vault_item(
        room_id: str,
        req: RoomVaultItemRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        room = await load_room_for_caller(caller, room_id)
        owner_bearer = bearer(request)
        await asyncio.to_thread(
            caller.hive.room_vault.ensure_room_key,
            room["room_id"],
            "owner",
            owner_bearer,
        )
        item = await asyncio.to_thread(
            caller.hive.room_vault.put_item,
            room["room_id"],
            text=req.text,
            metadata=req.metadata,
        )
        return item

    @app.get("/v1/rooms/{room_id}/data")
    async def list_room_vault_items(
        room_id: str,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        room = await load_room_for_caller(caller, room_id)
        items = await asyncio.to_thread(
            caller.hive.room_vault.list_items_for_bearer,
            room["room_id"],
            "owner",
            bearer(request),
        )
        return {"items": items}

    @app.delete("/v1/rooms/{room_id}")
    async def revoke_room(
        room_id: str,
        caller: Caller = Depends(requires_role("owner")),
    ):
        ok = await asyncio.to_thread(caller.hive.room_store.revoke, room_id)
        if not ok:
            raise HTTPException(404, f"room '{room_id}' not found")
        return {"status": "ok", "room_id": room_id}

    @app.post("/v1/rooms/{room_id}/trust")
    async def update_room_trust(
        room_id: str,
        req: RoomTrustUpdateRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner")),
    ):
        """Re-sign a room manifest with updated compose trust settings."""
        room = await load_room_for_caller(caller, room_id)
        manifest = dict(room["manifest"])
        manifest["trust"] = compose_trust_from_update(
            manifest.get("trust") or {},
            req,
        )
        manifest["updated_at"] = time.time()

        priv, _pub = derive_signing_keypair(bearer(request), caller.tenant_id)
        envelope = sign_manifest(manifest, priv)
        updated = await asyncio.to_thread(caller.hive.room_store.update, envelope)
        if not updated:
            raise HTTPException(404, f"room '{room_id}' not found")
        return {"room_id": room_id, "room": updated}

    @app.post("/v1/rooms/{room_id}/runs")
    async def submit_room_run(
        room_id: str,
        req: RoomRunRequest,
        request: Request,
        caller: Caller = Depends(requires_role("owner", "query")),
    ):
        room = await load_room_for_caller(caller, room_id)
        qreq = QueryRequest(
            query=req.query,
            room_id=room["room_id"],
            query_agent_id=req.query_agent_id,
            mediator_agent_id=req.mediator_agent_id,
            max_tokens=req.max_tokens,
            max_llm_calls=req.max_llm_calls,
            timeout_seconds=req.timeout_seconds,
            model=req.model,
            provider=req.provider,
        )
        qreq = apply_room_to_query_request(qreq, room)
        return await submit_query_run_for_request(
            qreq,
            caller,
            room,
            request=request,
            bearer=bearer(request),
        )
