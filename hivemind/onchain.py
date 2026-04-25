"""On-chain governance reads for the HivemindAppAuth contract.

Feedling's third binding, ported. Resolves a compose_hash against the
`isAppAllowed(bytes32)` view on the on-chain registry. The CLI uses
this to:

* **Auto-accept** any compose hash the contract owner has approved
  (no interactive y/N prompt, no TOFU).
* **Hard-abort** any compose hash the contract owner has revoked.

Reads go over plain HTTPS JSON-RPC — no web3.py dependency. A bad/stale
contract address, malformed RPC URL, or network error returns
``None`` ("unknown") so the caller can fall back to the CLI's
TOFU/change-prompt path. Never fail-open; unknown is not approved.

Selector: ``isAppAllowed(bytes32) -> bool`` = ``0x90144031``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# keccak256("isAppAllowed(bytes32)")[:4], without 0x prefix.
_IS_APP_ALLOWED_SELECTOR = "90144031"

# Sepolia testnet chain id.
ETH_SEPOLIA_CHAIN_ID = 11155111
ETHERSCAN_SEPOLIA = "https://sepolia.etherscan.io"


def _strip_0x(s: str) -> str:
    return s[2:] if s.startswith(("0x", "0X")) else s


def _pad32(hex_str: str) -> str:
    """Left-pad a hex string to 32 bytes / 64 hex chars. No 0x prefix."""
    h = _strip_0x(hex_str).lower()
    if len(h) > 64:
        raise ValueError(f"too long for bytes32: {len(h)} hex chars")
    return h.rjust(64, "0")


def _encode_call(compose_hash: str) -> str:
    return _IS_APP_ALLOWED_SELECTOR + _pad32(compose_hash)


def _decode_bool(raw: str) -> bool:
    """ABI-decode a ``bool`` return value from ``eth_call``."""
    h = _strip_0x(raw)
    # Pad to 64 hex chars in case the RPC trims leading zeros.
    return int(h.rjust(64, "0"), 16) == 1


def is_app_allowed(
    rpc_url: str,
    contract: str,
    compose_hash: str,
    *,
    timeout: float = 5.0,
) -> bool | None:
    """Return True/False if the contract answers; None on any error.

    Returning None (rather than raising) lets ``_require_trust`` fall
    back to the local TOFU/change-prompt path when the RPC is
    unreachable or the contract is misconfigured. The CLI UI surfaces
    the distinction to the user.
    """
    if not rpc_url or not contract or not compose_hash:
        return None
    data = "0x" + _encode_call(compose_hash)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": contract, "data": data},
            "latest",
        ],
    }
    try:
        resp = httpx.post(
            rpc_url,
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return None
    result = body.get("result")
    if not isinstance(result, str) or not result.startswith(("0x", "0X")):
        return None
    try:
        return _decode_bool(result)
    except (ValueError, TypeError):
        return None


def explorer_link(contract: str, chain_id: int = ETH_SEPOLIA_CHAIN_ID) -> str:
    """Return an Etherscan URL for ``contract`` on ``chain_id``."""
    if chain_id == ETH_SEPOLIA_CHAIN_ID:
        return f"{ETHERSCAN_SEPOLIA}/address/{contract}"
    return ""


__all__ = [
    "ETH_SEPOLIA_CHAIN_ID",
    "ETHERSCAN_SEPOLIA",
    "is_app_allowed",
    "explorer_link",
]
