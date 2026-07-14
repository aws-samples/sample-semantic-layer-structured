"""
Tier 3 supervisor loop — the agentic fallback (ontology / VKG query agent).

Progressive disclosure tiers the work by cost (see ``wiki/architecture.md`` and
the blog series):

  * Tier 1 — governed-metric lookup (<= 1 LLM call). Most dashboard questions.
  * Tier 2 — the Strands VKG graph: approximate -> refine -> translate (<= 5 LLM
    calls) over the ontology, executing grounded SPARQL via the Neptune gateway.
  * Tier 3 — THIS module: a bounded **supervisor loop** (many LLM calls) that is
    the free-flowing agentic fallback over the broader semantic layer, invoked
    ONLY when Tier 2 could not resolve the question (``wf.degraded`` set).

The loop mirrors the deep-research supervisor pattern (worker attempt -> judge
verdict -> guided retry, bounded by ``max_rounds``): a *worker* re-attempts the
question with the same semantic-layer tools but free-flowing reasoning, and a
*judge* decides whether the attempt actually answered the question or the loop
should retry with concrete guidance. Escalation into Tier 3 is deterministic —
Tier 2 either produced a grounded answer or it degraded; a clean Tier 2 run
(including an empty result set) never reaches here.

Design points:

  * The worker receives the **full context of the failed Tier 2 attempt** (the
    degraded terminal, the last generated SPARQL, the candidate IRIs) so it does
    not repeat the approach that already failed.
  * The judge's verdict is a small structured decision; its ``guidance`` is fed
    into the next worker round so retries are directed, not random.
  * **Fail-soft**: any error, or an exhausted loop, leaves ``accepted=False`` so
    the caller keeps Tier 2's honest degraded answer. Tier 3 only ever *replaces*
    the answer when the judge accepts a worker attempt.

Everything the loop needs is injected (``worker_fn`` / ``judge_fn``), so the
control flow is unit-testable without Bedrock or a live Neptune gateway.
``main.py`` wires the real Strands worker agent and judge model into these seams.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Bounded like the reference supervisor's ``max_researcher_iterations``: enough
# rounds for a guided retry to recover, few enough to bound Tier 3 spend.
DEFAULT_MAX_ROUNDS = 3


@dataclass
class WorkerAttempt:
    """One worker round's result, normalized for the judge and the caller.

    ``query`` holds the SPARQL the worker ran. ``error`` is set when the attempt
    failed to produce a clean run; a clean run has ``error=None`` and a non-empty
    ``query``. An empty ``results`` list on a clean run is a valid answer (the
    data is simply empty), not a failure.
    """

    answer: str = ""
    query: str = ""
    results: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    error: Optional[str] = None
    usage: dict = field(default_factory=dict)

    def ran_cleanly(self) -> bool:
        """True iff this attempt executed a query without error."""
        return self.error is None and bool(self.query)


@dataclass
class JudgeVerdict:
    """The supervisor judge's decision on a worker attempt."""

    sufficient: bool
    guidance: str = ""


@dataclass
class SupervisorOutcome:
    """Result of the Tier 3 supervisor loop.

    ``accepted`` is the only field the caller needs to decide whether to replace
    Tier 2's answer: it is True iff the judge accepted a worker attempt. On a
    fail-soft path (worker raised on the first round, or the loop exhausted its
    rounds without an accepted attempt) ``accepted`` is False and the caller
    keeps the Tier 2 degraded answer.
    """

    ran: bool
    accepted: bool
    answer: str = ""
    query: str = ""
    results: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    rounds: int = 0
    reason: str = ""
    usage: dict = field(default_factory=dict)


def parse_judge_verdict(raw: Any) -> JudgeVerdict:
    """Parse a judge model's response into a :class:`JudgeVerdict`.

    Accepts a dict (already-parsed structured output) or a JSON string, possibly
    wrapped in prose / markdown fences. Unparseable or shape-unexpected input is
    treated as *not sufficient* with empty guidance — the safe default keeps the
    loop from accepting an answer the judge never actually blessed.
    """
    obj: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        # Tolerate a ```json fenced block or leading/trailing prose around the
        # object by extracting the first {...} span.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return JudgeVerdict(sufficient=False)
        try:
            obj = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, TypeError):
            return JudgeVerdict(sufficient=False)
    if not isinstance(obj, dict):
        return JudgeVerdict(sufficient=False)

    val = obj.get("sufficient")
    if isinstance(val, bool):
        sufficient = val
    elif isinstance(val, str):
        sufficient = val.strip().lower() in ("1", "true", "yes", "sufficient")
    else:
        sufficient = False

    guidance = obj.get("guidance") or obj.get("gap") or ""
    return JudgeVerdict(sufficient=sufficient, guidance=str(guidance).strip())


_WORKER_PREAMBLE = (
    "You are the TIER 3 agentic fallback for an ontology (virtual knowledge "
    "graph) query agent. The structured Tier 2 workflow (topic routing -> "
    "disambiguate -> SPARQL) already tried to answer this question and could NOT "
    "resolve it. You have the same tools (ontology retrieval, SPARQL execution "
    "against the Neptune gateway) but you are NOT bound to a fixed sequence: "
    "reason freely over the ontology to find an approach the structured path "
    "missed.\n\n"
    "Rules:\n"
    "  - Study why Tier 2 failed (below) and do NOT repeat the same approach.\n"
    "  - If a prior query hit an unresolved class/predicate or a translation "
    "failure, retrieve more ontology context and re-derive the correct IRIs "
    "before querying.\n"
    "  - Copy class and predicate IRIs verbatim from the ontology — never guess "
    "or abbreviate them.\n"
    "  - You may reinterpret an ambiguous term, pick different classes, or take a "
    "different path through the ontology — whatever the semantic layer supports.\n"
    "  - Finish by executing SPARQL and returning a concrete, grounded answer."
)


def build_worker_task(
    question: str,
    failure_context: str,
    guidance: str,
    prior: Optional[WorkerAttempt],
) -> str:
    """Assemble the worker prompt for one round: request + failure/retry context."""
    parts = [
        _WORKER_PREAMBLE,
        "",
        "── Original question ──",
        question,
        "",
        "── Why Tier 2 could not resolve it ──",
        failure_context or "(no Tier 2 failure detail available)",
    ]
    if prior is not None:
        parts += [
            "",
            "── Your previous Tier 3 attempt (do not repeat) ──",
            f"SPARQL: {prior.query or '(none)'}",
            f"Outcome: {prior.error or 'ran, but the judge found it insufficient'}",
        ]
    if guidance:
        parts += [
            "",
            "── Judge guidance for this attempt ──",
            guidance,
        ]
    return "\n".join(parts)


def run_supervisor_loop(
    question: str,
    failure_context: str,
    worker_fn: Callable[[str], WorkerAttempt],
    judge_fn: Callable[[str, WorkerAttempt], JudgeVerdict],
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> SupervisorOutcome:
    """Run the bounded worker->judge->retry supervisor loop.

    Args:
        question: The natural-language user question.
        failure_context: A human-readable description of why Tier 2 degraded
            (terminal reason + last SPARQL + candidate hints), fed to the worker.
        worker_fn: Runs one free-flowing worker attempt for a task prompt and
            returns a normalized :class:`WorkerAttempt`. May raise; a raise on
            the FIRST round is fail-soft (``ran=False``).
        judge_fn: Judges whether a clean worker attempt actually answered the
            question, returning a :class:`JudgeVerdict` (``guidance`` directs the
            next round).
        max_rounds: Hard upper bound on worker attempts.

    Returns:
        A :class:`SupervisorOutcome`. ``accepted`` is True only when the judge
        blessed an attempt; otherwise the caller keeps the Tier 2 answer.
    """
    guidance = ""
    prior: Optional[WorkerAttempt] = None
    total_usage: Dict[str, Any] = {}
    ran_any = False

    for round_i in range(1, max_rounds + 1):
        task = build_worker_task(question, failure_context, guidance, prior)
        logger.info("[tier3] supervisor round %d/%d", round_i, max_rounds)
        try:
            attempt = worker_fn(task)
        except Exception as e:  # noqa: BLE001 — fail-soft: caller keeps Tier 2 answer
            logger.warning("[tier3] worker raised on round %d: %s", round_i, e)
            if not ran_any:
                return SupervisorOutcome(
                    ran=False, accepted=False, rounds=round_i - 1,
                    reason=f"worker raised before any clean attempt: {e}",
                    usage=total_usage,
                )
            # A later-round raise: stop and fall back to Tier 2 (accepted stays False).
            return SupervisorOutcome(
                ran=True, accepted=False, rounds=round_i - 1,
                reason=f"worker raised on round {round_i}: {e}",
                usage=total_usage,
            )

        ran_any = True
        _merge_usage(total_usage, attempt.usage)
        prior = attempt

        if not attempt.ran_cleanly():
            # Hard failure this round — direct the retry at the concrete error.
            guidance = (
                f"The previous attempt failed: {attempt.error}. Retrieve more "
                "ontology context and correct the query before retrying."
            )
            logger.info("[tier3] round %d did not run cleanly: %s", round_i, attempt.error)
            continue

        verdict = judge_fn(question, attempt)
        if verdict.sufficient:
            logger.info("[tier3] judge accepted attempt on round %d", round_i)
            return SupervisorOutcome(
                ran=True, accepted=True, answer=attempt.answer, query=attempt.query,
                results=attempt.results, columns=attempt.columns, rounds=round_i,
                reason="supervisor judge accepted the attempt", usage=total_usage,
            )
        # Clean run, but the judge wants more — carry its guidance into next round.
        guidance = verdict.guidance or (
            "The query ran but did not answer the question. Reconsider the "
            "class/predicate choices and try a different approach."
        )
        logger.info("[tier3] judge rejected round %d: %s", round_i, guidance)

    logger.info("[tier3] supervisor loop exhausted %d rounds without acceptance", max_rounds)
    return SupervisorOutcome(
        ran=True, accepted=False, rounds=max_rounds,
        answer=prior.answer if prior else "",
        query=prior.query if prior else "",
        reason=f"supervisor loop exhausted {max_rounds} rounds without an accepted answer",
        usage=total_usage,
    )


def _merge_usage(total: Dict[str, Any], add: Optional[Dict[str, Any]]) -> None:
    """Accumulate token-usage counters across rounds (best-effort, ints only)."""
    if not isinstance(add, dict):
        return
    for k, v in add.items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
