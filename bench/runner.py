"""Thin async HTTP client that talks to the live Hivemind server."""

from __future__ import annotations

import os
import time

import httpx


_DEFAULT_QUERY_TIMEOUT = float(os.environ.get("HIVEMIND_BENCH_QUERY_TIMEOUT", "600"))


async def run_query(
    query: str,
    base_url: str,
    *,
    scope_agent_id: str | None = None,
    mediator_agent_id: str | None = None,
    policy: str | None = None,
    api_key: str | None = None,
    timeout: float = _DEFAULT_QUERY_TIMEOUT,
    max_retries: int = 2,
) -> dict:
    """POST /v1/query and return {output, mediated, usage, latency_ms, error}.

    `policy` is an optional privacy/utility constraint string forwarded to
    the scope agent. When a bench scenario is being tested, pass the
    scenario's policy here so the scope agent can enforce it.

    Retries on 5xx with exponential backoff.
    """
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload: dict = {"query": query}
    if scope_agent_id:
        payload["scope_agent_id"] = scope_agent_id
    if mediator_agent_id:
        payload["mediator_agent_id"] = mediator_agent_id
    if policy:
        payload["policy"] = policy

    last_error = None
    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
                resp = await client.post(f"{base_url}/v1/query", json=payload)
            latency_ms = int((time.monotonic() - t0) * 1000)

            if resp.status_code >= 500 and attempt < max_retries:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                wait = 2 ** attempt
                import asyncio
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 400:
                return {
                    "output": "",
                    "mediated": False,
                    "usage": None,
                    "latency_ms": latency_ms,
                    "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
                }

            data = resp.json()
            return {
                "output": data.get("output", ""),
                "mediated": data.get("mediated", False),
                "usage": data.get("usage"),
                "latency_ms": latency_ms,
                "error": None,
            }
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                import asyncio
                await asyncio.sleep(2 ** attempt)
                continue
            return {
                "output": "",
                "mediated": False,
                "usage": None,
                "latency_ms": latency_ms,
                "error": last_error,
            }

    # Should not reach here, but just in case
    return {
        "output": "",
        "mediated": False,
        "usage": None,
        "latency_ms": 0,
        "error": last_error or "Max retries exceeded",
    }


async def store(
    sql: str,
    params: list | None = None,
    *,
    base_url: str,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """POST /v1/store and return the response."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        resp = await client.post(
            f"{base_url}/v1/store",
            json={"sql": sql, "params": params or []},
        )
        resp.raise_for_status()
        return resp.json()


async def health_check(base_url: str, api_key: str | None = None) -> dict:
    """GET /v1/health to verify server is running."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
            resp = await client.get(f"{base_url}/v1/health")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": str(exc)}
