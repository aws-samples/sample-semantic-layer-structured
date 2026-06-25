"""Red teaming evaluation runner for semantic layer query agents.

Executes adversarial attacks against the NL-to-SQL agents to validate
that Bedrock Guardrails hold under pressure. Uses Strands Evals
Red Teaming SDK with CrescendoStrategy (multi-turn escalation).

Usage:
    python -m tests.eval.red_team.run_red_team
"""

import asyncio
import logging
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


async def run_red_team(
    agent_factory: Callable[[], Agent],
    agent_name: str = "metadata_query_agent",
) -> bool:
    """Run the full red teaming suite against a single agent.

    Args:
        agent_factory: Zero-arg callable that returns a fresh Agent instance.
        agent_name: Human-readable name for reporting.

    Returns:
        True if the agent passed (zero breaches), False otherwise.
    """
    print(f"\n{'=' * 60}")
    print(f"  🔴 RED TEAM: {agent_name}")
    print(f"{'=' * 60}")

    # -------------------------------------------------------------------------
    # 1. Auto-generate adversarial cases from agent configuration
    # -------------------------------------------------------------------------
    print("\n  Generating adversarial cases from agent configuration...")
    try:
        # num_cases is PER risk category. Pass risk_categories explicitly so
        # the generator targets our configured set rather than inferring its
        # own. Total auto = AUTO_CASES_PER_CATEGORY × len(RISK_CATEGORIES).
        auto_cases = AdversarialCaseGenerator().generate_cases(
            agent=agent_factory(),
            risk_categories=RISK_CATEGORIES,
            num_cases=AUTO_CASES_PER_CATEGORY,
        )
        logger.info(f"Generated {len(auto_cases)} auto cases")
    except Exception as e:
        logger.error(f"Auto-generation failed: {e}. Proceeding with custom cases only.")
        auto_cases = []

    # -------------------------------------------------------------------------
    # 2. Combine with hand-authored FSI-specific cases
    # -------------------------------------------------------------------------
    all_cases = list(auto_cases) + list(CUSTOM_CASES)
    print(
        f"  Total cases: {len(all_cases)} "
        f"({len(auto_cases)} auto + {len(CUSTOM_CASES)} custom)"
    )

    if not all_cases:
        logger.error("No cases to run — aborting.")
        return False

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
    return handle_report(report, agent_name, MAX_ALLOWED_BREACHES)


async def run_all_agents() -> None:
    """Run red teaming against both query agents (Semantic RAG + VKG).

    Exits with code 1 if any agent has breaches.
    """
    from tests.eval.conftest import agent_factory, vkg_agent_factory

    results: dict[str, bool] = {}

    # Test Semantic RAG query agent
    results["metadata_query_agent"] = await run_red_team(
        agent_factory=agent_factory,
        agent_name="metadata_query_agent",
    )

    # Test VKG ontology query agent
    results["ontology_query_agent"] = await run_red_team(
        agent_factory=vkg_agent_factory,
        agent_name="ontology_query_agent",
    )

    # -------------------------------------------------------------------------
    # Final verdict
    # -------------------------------------------------------------------------
    print(f"\n{'=' * 60}")
    print("  FINAL RESULTS")
    print(f"{'=' * 60}")
    for name, passed in results.items():
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"  {status}: {name}")

    if all(results.values()):
        print("\n  ✅ RED TEAM PASSED — all guardrails held under adversarial pressure")
    else:
        print("\n  ❌ RED TEAM FAILED — breaches detected")
        print("  Review breach transcripts in tests/eval/red_team/reports/")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(run_all_agents(), timeout=EVAL_TIMEOUT_SECONDS))
    except asyncio.TimeoutError:
        logger.error(
            f"Red team run exceeded {EVAL_TIMEOUT_SECONDS}s timeout — failing the gate."
        )
        sys.exit(1)
