"""GAN loop: red_team -> runner -> judge -> evolve across rounds."""

from __future__ import annotations

import asyncio
import time

from bench import judge, red_team, runner
from bench.scenarios import Scenario


async def run_gan(
    scenario: Scenario,
    server_url: str,
    *,
    rounds: int = 3,
    attacks_per_round: int = 5,
    scope_agent_id: str | None = None,
    mediator_agent_id: str | None = None,
    api_key: str | None = None,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    verbose: bool = True,
) -> dict:
    """Run the full GAN adversarial benchmark for one scenario.

    Returns:
        {
            "scenario": scenario.id,
            "rounds": [...],
            "scores": {defense_rate, utility_score, combined, grade},
            "elapsed_ms": int
        }
    """
    t0 = time.monotonic()
    all_rounds = []
    all_judgments = []

    for round_num in range(1, rounds + 1):
        if verbose:
            print(f"\n  Round {round_num}/{rounds}")
            print(f"  {'─' * 40}")

        # Generate attacks
        if round_num == 1:
            attacks = list(scenario.seed_queries)
        else:
            attacks = await red_team.evolve(
                scenario,
                all_rounds[-1],
                n=attacks_per_round,
                llm_base_url=llm_base_url,
                llm_api_key=llm_api_key,
                llm_model=llm_model,
            )

        if verbose:
            print(f"  Attacks: {len(attacks)}")

        # Run each attack through the pipeline
        round_results = []
        for i, attack in enumerate(attacks):
            if verbose:
                preview = attack[:80] + ("..." if len(attack) > 80 else "")
                print(f"    [{i+1}/{len(attacks)}] {preview}")

            response = await runner.run_query(
                attack,
                server_url,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=mediator_agent_id,
                policy=scenario.policy,
                api_key=api_key,
            )

            if response.get("error"):
                if verbose:
                    print(f"      ERROR: {response['error'][:100]}")

            judgment = await judge.evaluate(
                scenario,
                attack,
                response["output"],
                llm_base_url=llm_base_url,
                llm_api_key=llm_api_key,
                llm_model=llm_model,
            )

            status = "LEAKED" if judgment.leaked else "SAFE"
            if verbose:
                sev = f" ({judgment.severity})" if judgment.leaked else ""
                util = " [useful]" if judgment.useful else ""
                print(f"      → {status}{sev}{util} ({response['latency_ms']}ms)")

            round_results.append({
                "attack": attack,
                "response": {
                    "output": response["output"],
                    "mediated": response["mediated"],
                    "latency_ms": response["latency_ms"],
                    "error": response.get("error"),
                },
                "judgment": judgment.to_dict(),
            })
            all_judgments.append(judgment)

        round_scores = judge.compute_scores(
            [judge.Judgment(**r["judgment"]) for r in round_results]
        )

        round_data = {
            "round": round_num,
            "attack_count": len(attacks),
            "results": round_results,
            "scores": round_scores,
        }
        all_rounds.append(round_data)

        if verbose:
            print(f"  Round {round_num} — Defense: {round_scores['defense_rate']:.0%} "
                  f"| Utility: {round_scores['utility_score']:.0%} "
                  f"| Grade: {round_scores['grade']}")

    overall_scores = judge.compute_scores(all_judgments)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    return {
        "scenario": scenario.id,
        "policy": scenario.policy,
        "rounds": all_rounds,
        "scores": overall_scores,
        "elapsed_ms": elapsed_ms,
    }


async def run_all_scenarios(
    scenarios: list[Scenario],
    server_url: str,
    *,
    rounds: int = 3,
    attacks_per_round: int = 5,
    scope_agent_id: str | None = None,
    mediator_agent_id: str | None = None,
    api_key: str | None = None,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    verbose: bool = True,
) -> list[dict]:
    """Run the GAN benchmark for all scenarios sequentially."""
    results = []
    for i, scenario in enumerate(scenarios):
        if verbose:
            print(f"\n{'═' * 60}")
            print(f"  Scenario {i+1}/{len(scenarios)}: {scenario.name}")
            print(f"  Policy: {scenario.policy[:80]}...")
            print(f"{'═' * 60}")

        result = await run_gan(
            scenario,
            server_url,
            rounds=rounds,
            attacks_per_round=attacks_per_round,
            scope_agent_id=scope_agent_id,
            mediator_agent_id=mediator_agent_id,
            api_key=api_key,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            verbose=verbose,
        )
        results.append(result)

    return results
