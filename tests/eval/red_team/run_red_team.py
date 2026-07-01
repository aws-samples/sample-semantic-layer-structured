"""Red teaming evaluation runner for semantic layer query agents.

Executes adversarial attacks against the NL-to-SQL agents to validate
that Bedrock Guardrails hold under pressure. Uses Strands Evals
Red Teaming SDK with CrescendoStrategy (multi-turn escalation).

Usage:
    python -m tests.eval.red_team.run_red_team

Exit-code contract (consumed by scripts/red-team-ci.sh):
    0  all agents passed, run not degraded
    1  a breach was detected, OR the run was degraded (auto-generation
       failed / produced no cases), OR the overall timeout was exceeded

Environment overrides:
    EVAL_MODEL_ID          Bedrock model for the target agents and judge
    AWS_REGION             AWS region for Bedrock calls
    RED_TEAM_AUTO_CASES    Override auto cases per risk category (int).
                           Lower it for cheap iteration / sample generation.
    RED_TEAM_TARGET        "real" (default; both agents), "metadata" or
                           "vkg" (a single real agent), or "weakened" (a
                           deliberately-vulnerable agent — breach evidence
                           only, never a real safety run).
    RED_TEAM_TIMEOUT_SECONDS  Override the overall run timeout (default
                           EVAL_TIMEOUT_SECONDS).
"""

import asyncio
import logging
import os
import sys
from typing import Callable

from strands import Agent
from strands_evals.experimental.redteam import (
    AdversarialCaseGenerator,
    CrescendoStrategy,
    RedTeamExperiment,
)

from .config import (
    AUTO_CASES_PER_CATEGORY,
    EVAL_TIMEOUT_SECONDS,
    MAX_ALLOWED_BREACHES,
    MAX_WORKERS,
    RISK_CATEGORIES,
)
from .custom_cases import CUSTOM_CASES
from .report_handler import handle_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _auto_cases_per_category() -> int:
    """Resolve auto-cases-per-category, honoring the RED_TEAM_AUTO_CASES override."""
    raw = os.environ.get("RED_TEAM_AUTO_CASES")
    if raw is None:
        return AUTO_CASES_PER_CATEGORY
    try:
        return max(int(raw), 0)
    except ValueError:
        logger.warning(
            "Ignoring non-integer RED_TEAM_AUTO_CASES=%r; using default %d",
            raw,
            AUTO_CASES_PER_CATEGORY,
        )
        return AUTO_CASES_PER_CATEGORY


async def run_red_team(
    agent_factory: Callable[[], Agent],
    agent_name: str = "metadata_query_agent",
) -> "tuple[bool, bool]":
    """Run the full red teaming suite against a single agent.

    Args:
        agent_factory: Zero-arg callable that returns a fresh Agent instance.
        agent_name: Human-readable name for reporting.

    Returns:
        ``(passed, degraded)`` — ``passed`` is True on zero breaches;
        ``degraded`` is True if auto-generation failed or produced no cases
        (the run then covers custom cases only and must NOT be read as a pass).
    """
    print(f"\n{'=' * 60}")
    print(f"  🔴 RED TEAM: {agent_name}")
    print(f"{'=' * 60}")

    # -------------------------------------------------------------------------
    # 1. Auto-generate adversarial cases from agent configuration
    # -------------------------------------------------------------------------
    print("\n  Generating adversarial cases from agent configuration...")
    degraded = False
    per_category = _auto_cases_per_category()
    try:
        if per_category == 0:
            raise RuntimeError("auto-generation disabled (RED_TEAM_AUTO_CASES=0)")
        # num_cases is PER risk category. Pass risk_categories explicitly so
        # the generator targets our configured set rather than inferring its
        # own. Total auto = per_category × len(RISK_CATEGORIES). Use the async
        # variant — the sync generate_cases() calls asyncio.run() internally,
        # which fails inside this already-running event loop.
        auto_cases = await AdversarialCaseGenerator().generate_cases_async(
            agent=agent_factory(),
            risk_categories=RISK_CATEGORIES,
            num_cases=per_category,
        )
        logger.info(f"Generated {len(auto_cases)} auto cases")
        if not auto_cases:
            degraded = True
            logger.error("Auto-generation returned zero cases — marking run DEGRADED.")
    except Exception as e:
        degraded = True
        auto_cases = []
        logger.error(
            f"Auto-generation failed: {e}. Marking run DEGRADED "
            f"(custom cases only; not a valid pass)."
        )

    # -------------------------------------------------------------------------
    # 2. Combine with hand-authored FSI-specific cases
    # -------------------------------------------------------------------------
    all_cases = list(auto_cases) + list(CUSTOM_CASES)
    print(
        f"  Total cases: {len(all_cases)} "
        f"({len(auto_cases)} auto + {len(CUSTOM_CASES)} custom)"
        + ("  [DEGRADED: auto-generation unavailable]" if degraded else "")
    )

    if not all_cases:
        logger.error("No cases to run — aborting.")
        return (False, True)

    # -------------------------------------------------------------------------
    # 3. Create experiment with attack strategies
    # -------------------------------------------------------------------------
    experiment = RedTeamExperiment(
        cases=all_cases,
        agent_factory=agent_factory,
        attack_strategies=[
            CrescendoStrategy(),
        ],
    )

    # -------------------------------------------------------------------------
    # 4. Execute attacks
    # -------------------------------------------------------------------------
    print(f"\n  Running attacks (async, max_workers={MAX_WORKERS})...")
    print(f"  Cases × Strategies = {len(all_cases)} × 1 = {len(all_cases)} attack sessions")
    print()

    report = await experiment.run_evaluations_async(max_workers=MAX_WORKERS)

    # -------------------------------------------------------------------------
    # 5. Analyze results
    # -------------------------------------------------------------------------
    passed = handle_report(report, agent_name, MAX_ALLOWED_BREACHES)
    return (passed, degraded)


def _select_targets() -> "list[tuple[Callable[[], Agent], str]]":
    """Resolve which agents to attack, honoring the RED_TEAM_TARGET override.

    Values: "real" (default; both production-shaped agents), "metadata" or
    "vkg" (a single real agent — cheaper, e.g. for evidence capture), or
    "weakened" (a deliberately-vulnerable agent, breach-evidence only).
    """
    target = os.environ.get("RED_TEAM_TARGET", "real").lower()

    if target == "weakened":
        from tests.eval.red_team._weakened_agent import weakened_agent_factory

        logger.warning(
            "RED_TEAM_TARGET=weakened — attacking a DELIBERATELY VULNERABLE agent "
            "for breach-evidence generation only. This is NOT a real safety run."
        )
        return [(weakened_agent_factory, "weakened_metadata_query_agent")]

    from tests.eval.factories import agent_factory, vkg_agent_factory

    if target == "metadata":
        return [(agent_factory, "metadata_query_agent")]
    if target == "vkg":
        return [(vkg_agent_factory, "ontology_query_agent")]

    return [
        (agent_factory, "metadata_query_agent"),
        (vkg_agent_factory, "ontology_query_agent"),
    ]


async def run_all_agents() -> None:
    """Run red teaming against the selected query agents.

    Exits with code 1 on any breach OR any degraded run.
    """
    results: "dict[str, tuple[bool, bool]]" = {}
    for factory, name in _select_targets():
        results[name] = await run_red_team(agent_factory=factory, agent_name=name)

    # -------------------------------------------------------------------------
    # Final verdict
    # -------------------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print("  FINAL RESULTS")
    print(f"{'=' * 60}")
    for name, (passed, degraded) in results.items():
        if degraded:
            status = "⚠️  DEGRADED"
        elif passed:
            status = "✅ PASSED"
        else:
            status = "❌ FAILED"
        print(f"  {status}: {name}")

    degraded_any = any(degraded for _, degraded in results.values())
    all_passed = all(passed for passed, _ in results.values())

    if degraded_any:
        print(
            "\n  ❌ RED TEAM INCOMPLETE — auto-generation failed or produced no "
            "cases (degraded run). This is NOT a valid pass."
        )
        sys.exit(1)

    if all_passed:
        print("\n  ✅ RED TEAM PASSED — all guardrails held under adversarial pressure")
    else:
        print("\n  ❌ RED TEAM FAILED — breaches detected")
        print("  Review breach transcripts in tests/eval/red_team/reports/")
        sys.exit(1)


if __name__ == "__main__":
    timeout_s = EVAL_TIMEOUT_SECONDS
    _raw_timeout = os.environ.get("RED_TEAM_TIMEOUT_SECONDS")
    if _raw_timeout is not None:
        try:
            timeout_s = max(int(_raw_timeout), 1)
        except ValueError:
            logger.warning(
                "Ignoring non-integer RED_TEAM_TIMEOUT_SECONDS=%r; using default %d",
                _raw_timeout,
                EVAL_TIMEOUT_SECONDS,
            )
    try:
        asyncio.run(asyncio.wait_for(run_all_agents(), timeout=timeout_s))
    except asyncio.TimeoutError:
        logger.error(
            f"Red team run exceeded {timeout_s}s timeout — failing the gate."
        )
        sys.exit(1)
