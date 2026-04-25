"""CLI entry point for the GAN adversarial benchmark.

Usage:
    # Load ChatGPT data into running server
    python -m bench.cli load --file /path/to/all_conversations.txt --url http://localhost:8100

    # Run full GAN benchmark (all scenarios, 3 rounds each)
    python -m bench.cli run --url http://localhost:8100 --rounds 3

    # Run single scenario
    python -m bench.cli run --url http://localhost:8100 --scenario pii_redaction

    # View results
    python -m bench.cli report --file bench/results/gan-latest.json
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv


def _load_env() -> dict:
    """Load .env and return LLM config."""
    load_dotenv()
    return {
        "llm_api_key": os.environ.get("HIVEMIND_LLM_API_KEY", ""),
        "llm_base_url": os.environ.get("HIVEMIND_LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        "llm_model": os.environ.get("HIVEMIND_LLM_MODEL", "anthropic/claude-sonnet-4.5"),
        # Tenant API key used to hit /v1/query and /v1/store.
        "api_key": os.environ.get("HIVEMIND_TENANT_KEY", ""),
    }


async def cmd_load(args: argparse.Namespace) -> None:
    """Load ChatGPT data into the server."""
    from bench.loader import parse_conversations, create_tables, load_conversations
    from bench.runner import health_check

    env = _load_env()

    print(f"Checking server at {args.url}...")
    health = await health_check(args.url, api_key=env["api_key"])
    if "error" in health:
        print(f"Server not reachable: {health['error']}")
        sys.exit(1)
    print(f"Server OK — {health.get('table_count', '?')} tables")

    print(f"\nParsing {args.file}...")
    conversations = parse_conversations(args.file)
    print(f"Parsed {len(conversations)} conversations")

    if args.max:
        print(f"Limiting to first {args.max} conversations")

    print("\nCreating tables...")
    await create_tables(args.url, api_key=env["api_key"])

    print("\nLoading data...")
    stats = await load_conversations(
        conversations,
        args.url,
        api_key=env["api_key"],
        batch_size=args.batch_size,
        max_convos=args.max,
    )

    print(f"\nDone! Loaded {stats['conversations_loaded']} conversations, "
          f"{stats['messages_loaded']} messages")


async def cmd_run(args: argparse.Namespace) -> None:
    """Run the GAN adversarial benchmark."""
    from bench.scenarios import ALL_SCENARIOS, get_scenario
    from bench.gan import run_gan, run_all_scenarios
    from bench.report import print_scenario_report, print_summary, export_json
    from bench.runner import health_check

    env = _load_env()

    if not env["llm_api_key"]:
        print("Error: HIVEMIND_LLM_API_KEY not set in .env")
        sys.exit(1)

    print(f"Checking server at {args.url}...")
    health = await health_check(args.url, api_key=env["api_key"])
    if "error" in health:
        print(f"Server not reachable: {health['error']}")
        sys.exit(1)
    print(f"Server OK — {health.get('table_count', '?')} tables")

    # Select scenarios
    if args.scenario:
        scenarios = [get_scenario(args.scenario)]
    else:
        scenarios = ALL_SCENARIOS

    print(f"\nRunning GAN benchmark: {len(scenarios)} scenarios, {args.rounds} rounds each")
    print(f"LLM: {env['llm_model']}")
    print(f"Scope agent: {args.scope_agent or 'default-scope'}")
    print(f"Mediator agent: {args.mediator_agent or 'default-mediator'}")

    results = await run_all_scenarios(
        scenarios,
        args.url,
        rounds=args.rounds,
        attacks_per_round=args.attacks,
        scope_agent_id=args.scope_agent or "default-scope",
        mediator_agent_id=args.mediator_agent or "default-mediator",
        api_key=env["api_key"],
        llm_base_url=env["llm_base_url"],
        llm_api_key=env["llm_api_key"],
        llm_model=env["llm_model"],
        verbose=not args.quiet,
    )

    # Print reports
    for result in results:
        print_scenario_report(result)
    print_summary(results)

    # Export JSON
    filepath = export_json(results)
    print(f"Results exported to: {filepath}")


async def cmd_report(args: argparse.Namespace) -> None:
    """View results from a previous run."""
    from bench.report import print_report_from_file

    if not os.path.exists(args.file):
        print(f"File not found: {args.file}")
        sys.exit(1)

    print_report_from_file(args.file)


def main():
    parser = argparse.ArgumentParser(
        prog="bench",
        description="GAN-style adversarial benchmark for Hivemind scope/mediator pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── load ──
    load_parser = subparsers.add_parser("load", help="Load ChatGPT data into the server")
    load_parser.add_argument("--file", required=True, help="Path to all_conversations.txt")
    load_parser.add_argument("--url", default="http://localhost:8100", help="Server URL")
    load_parser.add_argument("--max", type=int, default=None, help="Max conversations to load")
    load_parser.add_argument("--batch-size", type=int, default=50, help="Batch size for loading")

    # ── run ──
    run_parser = subparsers.add_parser("run", help="Run the GAN adversarial benchmark")
    run_parser.add_argument("--url", default="http://localhost:8100", help="Server URL")
    run_parser.add_argument(
        "--scenario",
        default=os.environ.get("HIVEMIND_BENCH_ONLY_SCENARIOS") or None,
        help=(
            "Run a single scenario by name. Defaults to "
            "$HIVEMIND_BENCH_ONLY_SCENARIOS if set (used by the remote "
            "parallel_ablations runner which only forwards HIVEMIND_BENCH_* "
            "env vars, not positional CLI flags)."
        ),
    )
    run_parser.add_argument("--rounds", type=int, default=3, help="Number of GAN rounds")
    run_parser.add_argument("--attacks", type=int, default=5, help="Attacks per round (rounds 2+)")
    run_parser.add_argument("--scope-agent", default=None, help="Scope agent ID")
    run_parser.add_argument("--mediator-agent", default=None, help="Mediator agent ID")
    run_parser.add_argument("--quiet", "-q", action="store_true", help="Suppress per-attack output")

    # ── report ──
    report_parser = subparsers.add_parser("report", help="View results from a previous run")
    report_parser.add_argument("--file", default="bench/results/gan-latest.json",
                               help="Path to results JSON")

    args = parser.parse_args()

    if args.command == "load":
        asyncio.run(cmd_load(args))
    elif args.command == "run":
        asyncio.run(cmd_run(args))
    elif args.command == "report":
        asyncio.run(cmd_report(args))


if __name__ == "__main__":
    main()
