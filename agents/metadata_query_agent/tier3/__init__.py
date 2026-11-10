"""Tier 3 — supervisor-loop agentic fallback (metadata query agent).

Invoked ONLY when the Tier 2 RAG graph degrades (``wf.degraded`` set): a bounded
worker->judge->retry loop that reasons freely over the semantic layer to recover
an answer the structured path missed. See :mod:`.supervisor` for the loop, and :mod:`.budget` for the per-round hard bound
that keeps one free-flowing worker round from spinning past the client timeout.
"""

from .budget import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_ROUND_SECONDS,
    WorkerBudget,
    WorkerBudgetExceeded,
)
from .supervisor import (
    DEFAULT_MAX_ROUNDS,
    JudgeVerdict,
    SupervisorOutcome,
    WorkerAttempt,
    build_worker_task,
    parse_judge_verdict,
    run_supervisor_loop,
)

__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_ROUND_SECONDS",
    "JudgeVerdict",
    "SupervisorOutcome",
    "WorkerAttempt",
    "WorkerBudget",
    "WorkerBudgetExceeded",
    "build_worker_task",
    "parse_judge_verdict",
    "run_supervisor_loop",
]
