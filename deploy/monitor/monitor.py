"""
monitor.py — Monitoring TEE for hivemind deploy governance.

Runs inside a separate dstack CVM. Watches NotarizedAppAuth for DeployRequested
events, logs them to IPFS, sends notifications, and calls notarize() on-chain.

Environment variables:
    CONTRACT_ADDRESS    — NotarizedAppAuth contract address on Base
    RPC_URL             — Base RPC endpoint (default: https://mainnet.base.org)
    IPFS_API            — IPFS HTTP API endpoint for pinning logs
    NOTIFY_WEBHOOK      — Webhook URL for notifications (Telegram, Slack, etc.)
    POLL_INTERVAL       — Seconds between event polls (default: 5)
"""

import json
import os
import sys
import time
import logging
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="[monitor] %(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# --- Config ---
CONTRACT = os.environ["CONTRACT_ADDRESS"]
RPC_URL = os.environ.get("RPC_URL", "https://mainnet.base.org")
IPFS_API = os.environ.get("IPFS_API", "http://127.0.0.1:5001")
NOTIFY_WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))

# DeployRequested(bytes32 indexed composeHash, uint256 timestamp)
DEPLOY_REQUESTED_TOPIC = (
    "0x" + "a6f4b3a7e3f2c1d0b9e8d7c6f5a4b3e2d1c0f9e8a7b6c5d4e3f2a1b0c9d8e7f6"
)
# Recompute: keccak256("DeployRequested(bytes32,uint256)")
# We'll compute it properly below.

# NotarizedAppAuth ABI (minimal)
NOTARIZE_SIG = "notarize(bytes32,bytes)"

def keccak256(text: str) -> str:
    """Compute keccak256 of a string. Uses pysha3 or hashlib."""
    try:
        import sha3
        return "0x" + sha3.keccak_256(text.encode()).hexdigest()
    except ImportError:
        from Crypto.Hash import keccak
        k = keccak.new(digest_bits=256)
        k.update(text.encode())
        return "0x" + k.hexdigest()


# Compute event topic at import time
DEPLOY_REQUESTED_TOPIC = keccak256("DeployRequested(bytes32,uint256)")


def eth_rpc(method: str, params: list) -> dict:
    """Make a JSON-RPC call to the Ethereum node."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }).encode()
    req = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    if "error" in result:
        raise RuntimeError(f"RPC error: {result['error']}")
    return result["result"]


def get_notary_key() -> bytes:
    """Derive the notary signing key from dstack KMS via dstack-sdk."""
    from dstack_sdk import DstackClient

    client = DstackClient()
    result = client.get_key("/notary/signer", purpose="signing")

    key_hex = result.key
    if not key_hex or not isinstance(key_hex, str):
        raise RuntimeError(f"KMS returned invalid key (type={type(key_hex).__name__})")
    if len(key_hex) < 64:
        raise RuntimeError(f"KMS key too short: {len(key_hex)} hex chars (need >=64)")
    return bytes.fromhex(key_hex[:64])


def pin_to_ipfs(log_entry: dict) -> str:
    """Pin a JSON log entry to IPFS. Returns the CID."""
    payload = json.dumps(log_entry).encode()
    req = urllib.request.Request(
        f"{IPFS_API}/api/v0/add?pin=true",
        data=payload,
        headers={"Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result["Hash"]


def send_notification(compose_hash: str, cid: str):
    """Send a webhook notification about the deploy."""
    if not NOTIFY_WEBHOOK:
        return
    payload = json.dumps({
        "text": f"hivemind deploy notarized\nhash: {compose_hash}\nlog: ipfs://{cid}",
    }).encode()
    req = urllib.request.Request(
        NOTIFY_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning("Notification failed: %s", e)


def send_notarize_tx(compose_hash: str, log_cid: str, private_key: bytes):
    """Call notarize(composeHash, logCID) on-chain."""
    try:
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        account = Account.from_key(private_key)

        # Minimal ABI for notarize
        abi = [{
            "name": "notarize",
            "type": "function",
            "inputs": [
                {"name": "composeHash", "type": "bytes32"},
                {"name": "logCID", "type": "bytes"},
            ],
            "outputs": [],
        }]
        contract = w3.eth.contract(address=CONTRACT, abi=abi)
        tx = contract.functions.notarize(
            bytes.fromhex(compose_hash[2:]) if compose_hash.startswith("0x") else bytes.fromhex(compose_hash),
            log_cid.encode(),
        ).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 100_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info("notarize tx sent: %s", tx_hash.hex())
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        log.info("notarize confirmed in block %d", receipt["blockNumber"])
    except ImportError:
        log.error("web3 not installed — cannot send notarize tx")
        raise


def poll_events(from_block: int) -> tuple[list, int]:
    """Poll for DeployRequested events from from_block to latest."""
    latest = int(eth_rpc("eth_blockNumber", []), 16)
    if from_block > latest:
        return [], latest

    logs = eth_rpc("eth_getLogs", [{
        "address": CONTRACT,
        "topics": [DEPLOY_REQUESTED_TOPIC],
        "fromBlock": hex(from_block),
        "toBlock": hex(latest),
    }])
    return logs, latest


def main():
    log.info("Starting monitoring TEE")
    log.info("Contract: %s", CONTRACT)
    log.info("RPC: %s", RPC_URL)

    # Derive notary key (retry up to 3 times)
    private_key = None
    for attempt in range(3):
        try:
            private_key = get_notary_key()
            break
        except Exception as e:
            log.error("KMS key derivation failed (attempt %d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(5)
    if private_key is None:
        log.error("FATAL: Could not derive notary key after 3 attempts — exiting")
        sys.exit(1)
    log.info("Notary key derived from KMS")

    # Start from latest block
    from_block = int(eth_rpc("eth_blockNumber", []), 16)
    log.info("Watching from block %d", from_block)

    while True:
        try:
            events, latest = poll_events(from_block)
            for event in events:
                compose_hash = event["topics"][1]  # indexed bytes32
                block_num = int(event["blockNumber"], 16)
                log.info("DeployRequested: hash=%s block=%d", compose_hash, block_num)

                # 1. Log to IPFS
                log_entry = {
                    "event": "DeployRequested",
                    "composeHash": compose_hash,
                    "block": block_num,
                    "contract": CONTRACT,
                    "timestamp": int(time.time()),
                }
                cid = pin_to_ipfs(log_entry)
                log.info("Logged to IPFS: %s", cid)

                # 2. Notify
                send_notification(compose_hash, cid)

                # 3. Notarize on-chain
                send_notarize_tx(compose_hash, cid, private_key)

            from_block = latest + 1
        except Exception as e:
            log.error("Poll error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
