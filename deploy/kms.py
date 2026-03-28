#!/usr/bin/env python3
"""KMS key derivation helper using dstack-sdk.

NOTE: KMS integration temporarily disabled to avoid issues in production.
      This script now exits with an error directing callers to use env vars instead.

Replaces raw curl | python3 one-liners with proper SDK usage,
including error handling and key format validation.

Usage:
    python3 kms.py <path> [--purpose PURPOSE] [--first N]

Examples:
    python3 kms.py /hivemind/db-password --purpose authentication --first 32
    python3 kms.py /hivemind/backup --purpose encryption --first 64
    python3 kms.py /notary/signer --purpose signing --first 64

Prints the hex-encoded key (or first N hex chars) to stdout.
Exits non-zero with message on stderr on any failure.
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Derive a key from dstack KMS")
    parser.add_argument("path", help="KMS derivation path (e.g. /hivemind/db-password)")
    parser.add_argument("--purpose", default=None, help="Key purpose for signature chain")
    parser.add_argument("--first", type=int, default=None, help="Truncate to first N hex chars")
    args = parser.parse_args()

    # --- KMS temporarily disabled ---
    print(
        "FATAL: KMS integration is temporarily disabled. "
        "Please set DB_PASS / WALG_LIBSODIUM_KEY via environment variables directly.",
        file=sys.stderr,
    )
    sys.exit(1)

    # try:
    #     from dstack_sdk import DstackClient
    # except ImportError:
    #     print("FATAL: dstack-sdk not installed (pip install dstack-sdk)", file=sys.stderr)
    #     sys.exit(1)
    #
    # try:
    #     client = DstackClient()
    #     kwargs = {}
    #     if args.purpose:
    #         kwargs["purpose"] = args.purpose
    #     result = client.get_key(args.path, **kwargs)
    # except Exception as e:
    #     print(f"FATAL: KMS call failed: {e}", file=sys.stderr)
    #     sys.exit(1)
    #
    # key_hex = result.key
    # if not key_hex or not isinstance(key_hex, str):
    #     print(f"FATAL: KMS returned invalid key (type={type(key_hex).__name__})", file=sys.stderr)
    #     sys.exit(1)
    #
    # if args.first:
    #     if len(key_hex) < args.first:
    #         print(
    #             f"FATAL: KMS key too short: {len(key_hex)} hex chars, need {args.first}",
    #             file=sys.stderr,
    #         )
    #         sys.exit(1)
    #     key_hex = key_hex[:args.first]
    #
    # print(key_hex)


if __name__ == "__main__":
    main()
