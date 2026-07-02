"""Report handler for red team evaluation results.

Processes RedTeamReport objects, displays results, saves breach
transcripts for remediation, and returns pass/fail for CI gating.

The Strands Evals RedTeamReport exposes:
  - report.display()            -> prints the breach matrix + worst-first table
  - report.failed_cases         -> breached AttackResults only, worst-first
  - report.attack_results()     -> every AttackResult (breached + defended)
  - report.by_risk_category()   -> list[GroupedSummary] (group_name, count,
                                    avg_score, pass_rate)
  - report.by_strategy()        -> list[GroupedSummary]

Each AttackResult carries: case_name, risk_category, strategy, severity,
score, passed (True == defended), reason, and conversation.
"""

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent / "reports"


def handle_report(
    report: Any,
    agent_name: str,
    max_allowed_breaches: int,
) -> bool:
    """Process a red team report and determine pass/fail.

    Args:
        report: RedTeamReport from the experiment run.
        agent_name: Name of the agent that was tested.
        max_allowed_breaches: Maximum breaches before CI fails.

    Returns:
        True if the agent passed (breaches <= max_allowed), False otherwise.
    """
    # Display the full report (breach matrix + worst-first table)
    report.display()

    # A breach is a non-passing attack. failed_cases is breached-only,
    # worst-first; it is the authoritative breach list.
    breached = list(report.failed_cases)
    total_breaches = len(breached)

    breaches_by_category = Counter(_attr(r, "risk_category") for r in breached)
    breaches_by_strategy = Counter(_attr(r, "strategy") for r in breached)

    # Per-category summary. Enumerate every tested category from the rollup
    # so categories with zero breaches still show a ✅.
    print(f"\n  Results for {agent_name}:")
    print(f"  {'─' * 50}")
    for group in report.by_risk_category():
        category = _attr(group, "group_name")
        breach_count = breaches_by_category.get(category, 0)
        status = "❌" if breach_count else "✅"
        print(f"  {status} {category}: {breach_count} breach(es)")

    print(f"\n  Total breaches: {total_breaches} (max allowed: {max_allowed_breaches})")

    # Save breach transcripts for analysis and remediation
    if total_breaches > 0:
        _save_breach_report(
            agent_name,
            breached,
            dict(breaches_by_category),
            dict(breaches_by_strategy),
            total_breaches,
        )

    passed = total_breaches <= max_allowed_breaches
    if passed:
        logger.info(f"✅ {agent_name}: PASSED — guardrails held ({total_breaches} breaches)")
    else:
        logger.warning(
            f"❌ {agent_name}: FAILED — {total_breaches} breaches "
            f"(max allowed: {max_allowed_breaches})"
        )

    return passed


def _attr(obj: Any, name: str) -> Any:
    """Read an attribute or mapping key, tolerating either shape."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _save_breach_report(
    agent_name: str,
    breached: list,
    breaches_by_category: dict,
    breaches_by_strategy: dict,
    total_breaches: int,
) -> None:
    """Save breach transcripts as JSON for remediation analysis."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{agent_name}_{timestamp}_breaches.json"
    filepath = REPORTS_DIR / filename

    breach_data = {
        "agent": agent_name,
        "timestamp": timestamp,
        "total_breaches": total_breaches,
        "summary": {
            "by_category": breaches_by_category,
            "by_strategy": breaches_by_strategy,
        },
        "breaches": [
            {
                "case_name": _attr(r, "case_name"),
                "risk_category": _attr(r, "risk_category"),
                "strategy": _attr(r, "strategy"),
                "severity": _attr(r, "severity"),
                "score": _attr(r, "score"),
                "reason": _attr(r, "reason"),
                "conversation": _attr(r, "conversation"),
            }
            for r in breached
        ],
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(breach_data, f, indent=2, default=str)

    print(f"\n  ⚠️  Breach transcripts saved: {filepath}")
    print("  Review conversations to identify guardrail gaps and plan mitigations.")
    logger.info(f"Breach report written to {filepath}")
