"""Tier 3 — supervisor-loop agentic fallback (ontology / VKG query agent).

Invoked ONLY when the Tier 2 VKG graph degrades (``wf.degraded`` set): a bounded
worker->judge->retry loop that reasons freely over the ontology / virtual
knowledge graph to recover an answer the structured SPARQL path missed. See
:mod:`.supervisor` for the loop.
"""

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
    "JudgeVerdict",
    "SupervisorOutcome",
    "WorkerAttempt",
    "build_worker_task",
    "parse_judge_verdict",
    "run_supervisor_loop",
]
