"""Tier-2 Hybrid resolution as a Strands multi-agent Graph workflow.

Wires the Hybrid query flow into a single Strands ``Graph``:

    Phase 1  recall router          (KNN retrieval -> candidate tables)
    Phase 2  term disambiguation    (ambiguous term -> clarification)
    Phase 3  slice builder + judge loop
    Phase 3b slice disambiguation   (slice-level collisions -> clarification)
    Phase 4  SQL generate           (NL -> SQL, negative-constraint aware)
    Phase 5  ontology-verified grounding gate + bounded execution

Phase 5 is the Hybrid-specific part. Where the RAG agent grounds SQL by flat
set-membership against a JSON slice, the Hybrid agent fetches the thin-VKG
structural graph from Neptune, bridges the generated SQL into ontology
``(subject_class, predicate)`` triples, and verifies each triple against the
predicate's ``rdfs:domain`` in that slice. A predicate used against the wrong
class (or a predicate absent from the slice) is a grounding miss that loops
back to Phase 4 (regenerate SQL with the miss threaded in as a negative
constraint). The loop is bounded by ``MAX_GROUNDING_ROUNDS``; exceeding it
abstains via the degraded terminal rather than executing un-grounded SQL. A
fetch failure fails LOUD (``hybrid_verify_unavailable``) — never a silent
single-engine answer.

Copy-and-own isolation rule (``tests/unit/test_hybrid_isolation.py``): this
module imports ONLY from within ``agents.semantic_layer_query_agent`` and from
``strands`` — never ``agents.shared`` or a sibling agent. The mode-agnostic
:class:`WorkflowContext`, the :class:`_FnNode` adapter, the usage helpers, and
the loop ceilings are therefore COPIED here (not imported from the shared
engine).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from strands.multiagent.base import MultiAgentBase, MultiAgentResult, Status
from strands.multiagent.graph import GraphBuilder

from .grounding import verify_sql_grounding
from .ontop_mappings import build_inverse_index
from .sql_ontology_bridge import sql_to_triples

logger = logging.getLogger(__name__)


# Loop ceilings — keep the graph from spinning. The Phase 5 grounding back-edge
# is bounded independently, and the graph carries a global node-execution
# backstop on top.
MAX_GROUNDING_ROUNDS = 2
MAX_NODE_EXECUTIONS = 40
MAX_PHASE3_ROUNDS = 3


# ---------------------------------------------------------------------------
# Shared workflow state (copied verbatim from the shared tier-2 engine per the
# copy-and-own isolation rule — do NOT import agents.shared).
# ---------------------------------------------------------------------------
@dataclass
class WorkflowContext:
    """Mutable state threaded through every phase of one Tier 2 resolution.

    A single instance is created per :func:`run_hybrid_workflow` call; the phase
    node functions and the conditional-edge predicates all close over it.

    The generated query is stored on the canonical ``sql`` field; ``.query`` is a
    mode-neutral alias.

    Attributes:
        question: The natural-language user question.
        namespace: Semantic-layer namespace used for KB / Neptune scoping.
        kb_id: Bedrock Knowledge Base id (Phase 1 retrieval); ``""`` for VKG.
        database_name: Athena database (extracted from Phase 1 metadata).
        catalog_id: Athena catalog (extracted from Phase 1 metadata).
        candidates: Phase 1 candidate ids — ``database.table`` ids, ranked by score.
        slice_text: Serialized schema/ontology slice (Phase 3 output).
        disambiguation: Resolved term bindings recorded by Phase 2/3b for the
            Phase 4 generator prompt.
        needs_clarification: Clarification payload when Phase 2 or 3b cannot
            resolve ambiguity heuristically; ``None`` otherwise.
        clarification_source: ``"phase2"`` | ``"phase3b"`` (diagnostic only).
        clarification_resolution: Resolution object when this turn answers a
            prior clarification; ``None`` on a normal turn.
        sql: Generated + syntax-validated query (Phase 4 output).
        phase3_rounds: Number of Phase 3 build/expand iterations executed.
        degraded: Non-None when a phase exited via a degraded path
            (``"phase1_empty"``, ``"phase3_max_rounds"``, ``"grounding_unresolved"``,
            ``"hybrid_verify_unavailable"``).
        degraded_detail: Optional human-readable detail for a degraded terminal.
        execution_result: Phase 5 execution result dict.
        grounding_rounds: Number of Phase 5 grounding loop-backs taken.
        grounding_missing: Ontology miss tags the grounding gate found — routes a
            regenerate back-edge to Phase 4.
        grounding_feedback: Negative-constraint string the grounding gate hands
            the generator on a Phase 4 regenerate back-edge.
        grounding_route: Hybrid back-edge selector (unused here — Hybrid always
            targets Phase 4; kept for context-shape parity).
        kb_sources: Source citations.
        usage: Running ``{inputTokens, outputTokens, totalTokens}`` total.
        phase_sink: Live per-phase trace sink; ``None`` disables tracing.
    """

    question: str
    namespace: str
    kb_id: str = ""
    database_name: str = ""
    catalog_id: str = ""
    candidates: List[str] = field(default_factory=list)
    slice_text: str = ""
    disambiguation: Dict[str, Any] = field(default_factory=dict)
    needs_clarification: Optional[Dict[str, Any]] = None
    clarification_source: Optional[str] = None
    clarification_resolution: Optional[Any] = None
    prior_clarification_options: List[Dict[str, Any]] = field(default_factory=list)
    prior_clarification_terms: List[str] = field(default_factory=list)
    sql: str = ""
    phase3_rounds: int = 0
    degraded: Optional[str] = None
    degraded_detail: Optional[str] = None
    execution_result: Dict[str, Any] = field(default_factory=dict)
    grounding_rounds: int = 0
    grounding_missing: List[str] = field(default_factory=list)
    grounding_feedback: str = ""
    grounding_route: Optional[str] = None
    kb_sources: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, int] = field(
        default_factory=lambda: {"inputTokens": 0, "outputTokens": 0,
                                 "totalTokens": 0})
    phase_sink: Optional[Callable[[Optional[int], str, Dict[str, Any]], None]] = None
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def query(self) -> str:
        """Mode-neutral alias for the generated query (SQL)."""
        return self.sql

    @query.setter
    def query(self, value: str) -> None:
        self.sql = value


def _emit_phase(ctx: WorkflowContext, *, phase: Optional[int], action: str,
                **payload: Any) -> None:
    """Forward a per-phase trace event to the context's sink (fail-soft).

    Args:
        ctx: The active workflow context.
        phase: Phase number (1-5) or ``None``.
        action: ``"phase_start"`` or ``"phase_result"``.
        **payload: Extra fields merged into the event.

    Returns:
        None.
    """
    sink = ctx.phase_sink
    if sink is None:
        return
    try:
        sink(phase, action, dict(payload))
    except Exception as exc:  # noqa: BLE001 — tracing must never break a query
        logger.debug("phase_sink failed (non-fatal) phase=%s action=%s: %s",
                     phase, action, exc)


# The token-usage keys we track (cache-inclusive, mirroring the shared engine).
_USAGE_KEYS = (
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "cacheReadInputTokens",
    "cacheWriteInputTokens",
)


def add_usage(ctx: WorkflowContext, delta: Dict[str, int]) -> None:
    """Accumulate a per-call usage ``delta`` into the context's running total.

    Args:
        ctx: The active workflow context (mutated in place).
        delta: The per-call usage dict to fold in.

    Returns:
        None.
    """
    for key in _USAGE_KEYS:
        if key in delta or key in ctx.usage:
            ctx.usage[key] = ctx.usage.get(key, 0) + int(delta.get(key, 0) or 0)


# ---------------------------------------------------------------------------
# Deterministic-phase node adapter (copied from the shared engine).
# ---------------------------------------------------------------------------
class _FnNode(MultiAgentBase):
    """Wrap a deterministic ``(ctx) -> None`` phase function as a Graph node.

    The phase function mutates the shared :class:`WorkflowContext` in place. The
    blocking body runs via ``asyncio.to_thread`` so boto3 / sqlglot / rdflib /
    Bedrock calls don't stall the graph's event loop.
    """

    def __init__(self, *, name: str, fn: Callable[[WorkflowContext], None],
                 ctx: WorkflowContext) -> None:
        """Construct the node.

        Args:
            name: Node id (also used in logs).
            fn: The phase function to run; receives the shared context.
            ctx: The shared workflow context instance.

        Returns:
            None.
        """
        super().__init__()
        self._name = name
        self._fn = fn
        self._ctx = ctx

    async def invoke_async(self, task: Any,
                           invocation_state: Optional[Dict[str, Any]] = None,
                           **kwargs: Any) -> MultiAgentResult:
        """Run the phase function in a worker thread; return a COMPLETED result.

        Args:
            task: The graph task payload (unused — state flows via the shared ctx).
            invocation_state: Strands invocation state (unused).
            **kwargs: Extra Strands keyword arguments (unused).

        Returns:
            A COMPLETED :class:`MultiAgentResult`.
        """
        await asyncio.to_thread(self._fn, self._ctx)
        return MultiAgentResult(status=Status.COMPLETED)


# ---------------------------------------------------------------------------
# Injected phase dependencies.
# ---------------------------------------------------------------------------
@dataclass
class HybridDeps:
    """Injected implementations the Hybrid phase functions call into.

    Attributes:
        router: Phase 1 recall router (``find_candidates(question, namespace)``).
        builder: Phase 3 slice builder (``build`` / ``is_sufficient`` / ``expand``).
        generator: Phase 4 SQL generator (``generate`` + ``last_usage``).
        fetch: Phase 5 structural-graph fetcher
            (``get(namespace, candidates) -> rdflib.Graph``).
        run_execution: Callable ``(ctx) -> dict`` that runs the bounded execution
            agent on the grounded SQL and returns the parsed result dict.
        vocab: Vocabulary namespace prefix (incl. trailing ``#``/``/``) whose
            ``mapsToTable`` / ``mapsToColumn`` terms the inverse index reads.
        dialect: ``sqlglot`` dialect identifier (e.g. ``"athena"``) for the bridge.
        recall_resolver: Optional ``(term, candidate_ids) -> Optional[str]`` that
            resolves an ambiguous term from prior lessons. ``None`` disables recall.
        answer_emitter: Optional ``(ctx) -> None`` eval-telemetry hook the graph's
            clarify / degraded TERMINAL nodes call so the final-answer span is
            emitted while the graph's multiagent span is still the active
            (recording) OTEL context — the only position the SESSION harvester
            treats as the conversation's final answer. A clarify or degraded turn
            makes no model call of its own, so without this it emits no recognized
            turn span and is DROPPED from the SESSION ``{context}``. ``None`` (the
            default) disables in-graph answer telemetry.
    """

    router: Any
    builder: Any
    generator: Any
    fetch: Any
    run_execution: Callable[[WorkflowContext], Dict[str, Any]]
    vocab: str
    dialect: str
    recall_resolver: Optional[Callable[..., Optional[str]]] = None
    answer_emitter: Optional[Callable[[WorkflowContext], None]] = None


# ---------------------------------------------------------------------------
# Phase node functions — built as closures over injected deps + shared ctx.
# ---------------------------------------------------------------------------
def _make_phase1(ctx: WorkflowContext,
                 deps: HybridDeps) -> Callable[[WorkflowContext], None]:
    """Build the Phase 1 node — recall router: KNN retrieval -> candidate tables.

    Args:
        ctx: The shared workflow context.
        deps: Injected phase implementations.

    Returns:
        The Phase 1 node function.
    """
    def phase1(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=1, action="phase_start")
        ctx.candidates = deps.router.find_candidates(
            question=ctx.question, namespace=ctx.namespace,
        )
        # Pull the Athena execution context (database) from the top-ranked
        # candidate id (``database.table``) so Phase 5 execution needs no second
        # retrieval. The first candidate leads the relevance order.
        if ctx.candidates and not ctx.database_name:
            top = ctx.candidates[0]
            if "." in top:
                ctx.database_name = top.split(".", 1)[0]
        if not ctx.candidates:
            ctx.degraded = "phase1_empty"
        _emit_phase(ctx, phase=1, action="phase_result",
                    candidateCount=len(ctx.candidates),
                    candidates=list(ctx.candidates), candidateKind="table",
                    degraded=ctx.degraded)
    return phase1


def _make_phase2(ctx: WorkflowContext,
                 deps: HybridDeps) -> Callable[[WorkflowContext], None]:
    """Build the Phase 2 node — term-level disambiguation over the candidates.

    Args:
        ctx: The shared workflow context.
        deps: Injected phase implementations.

    Returns:
        The Phase 2 node function.
    """
    from .disambiguation import analyze_terms, build_clarification

    def phase2(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=2, action="phase_start")
        # The Hybrid router returns only ranked candidate ids (no structured
        # score payload), so synthesize the analyze_terms ``candidates`` shape
        # from them. Scores are unknown at this seam; disambiguation still
        # resolves federated name collisions and multi-domain spans.
        structured = {
            "candidates": [{"table_id": tid, "score": 1.0} for tid in ctx.candidates],
        }
        resolved_names = set(
            getattr(ctx.clarification_resolution, "chosen_names", []) or []
        )
        analysis = analyze_terms(question=ctx.question, structured=structured,
                                 recall_resolver=deps.recall_resolver,
                                 resolved_names=resolved_names)
        ctx.disambiguation = analysis.get("mappings", {})
        if not analysis.get("can_proceed", True):
            items = analysis.get("ambiguities") or []
            if not items:
                items = [{"term": ctx.question[:60], "matches": [
                    {"table": tid.split(".")[-1],
                     "database": tid.split(".")[0] if "." in tid else "",
                     "column": ""} for tid in ctx.candidates[:5]]}]
            ctx.needs_clarification = build_clarification(items=items)
            ctx.clarification_source = "phase2"
        _emit_phase(ctx, phase=2, action="phase_result",
                    status=analysis.get("status"),
                    ambiguities=analysis.get("ambiguities") or [])
    return phase2


def _make_phase3(ctx: WorkflowContext,
                 deps: HybridDeps) -> Callable[[WorkflowContext], None]:
    """Build the Phase 3 node — slice builder + judge loop.

    Builds the slice from the Phase 1 candidates and expands it (via the judge's
    ``missing`` list) until sufficient or the round ceiling. The Phase 5
    grounding back-edge targets Phase 4 (SQL regeneration), not this node.

    Args:
        ctx: The shared workflow context.
        deps: Injected phase implementations.

    Returns:
        The Phase 3 node function.
    """
    def phase3(_c: WorkflowContext) -> None:
        visit_round = ctx.phase3_rounds + 1
        _emit_phase(ctx, phase=3, action="phase_start", round=visit_round)
        ctx.slice_text = deps.builder.build(
            candidates=ctx.candidates, namespace=ctx.namespace,
            question=ctx.question,
        )
        rounds = 1
        judge_missing: Optional[List[str]] = None
        while True:
            ok, judge_missing = deps.builder.is_sufficient(
                slice_text=ctx.slice_text, question=ctx.question,
            )
            if ok:
                break
            if rounds >= MAX_PHASE3_ROUNDS:
                ctx.degraded = "phase3_max_rounds"
                unmet = [m for m in (judge_missing or []) if m]
                if unmet:
                    shown = ", ".join(unmet[:5])
                    ctx.degraded_detail = (
                        "I found relevant tables but the data needed to answer "
                        "this question isn't available in this semantic layer. "
                        f"Missing: {shown}."
                    )
                break
            # Expand the slice with the judge's missing tables. When expand() can
            # add nothing fetchable it returns a byte-identical slice — re-judging
            # would repeat the verdict, so degrade now rather than spin.
            prev_slice = ctx.slice_text
            ctx.slice_text = deps.builder.expand(
                slice_text=ctx.slice_text, missing=judge_missing or [],
            )
            if ctx.slice_text == prev_slice:
                ctx.degraded = "phase3_max_rounds"
                unmet = [m for m in (judge_missing or []) if m]
                if unmet:
                    shown = ", ".join(unmet[:5])
                    ctx.degraded_detail = (
                        "I found relevant tables but the data needed to answer "
                        "this question isn't available in this semantic layer. "
                        f"Missing: {shown}."
                    )
                break
            rounds += 1
        ctx.phase3_rounds += rounds
        delta = dict(getattr(deps.builder, "judge_usage", {}) or {})
        add_usage(ctx, delta)
        _emit_phase(ctx, phase=3, action="phase_result",
                    round=visit_round, judgeRounds=rounds,
                    sufficient=(ctx.degraded is None),
                    tableCount=len(ctx.candidates), slice=ctx.slice_text)
    return phase3


def _make_phase3b(ctx: WorkflowContext,
                  deps: HybridDeps) -> Callable[[WorkflowContext], None]:
    """Build the Phase 3b node — slice-level disambiguation guard.

    Args:
        ctx: The shared workflow context.
        deps: Injected phase implementations.

    Returns:
        The Phase 3b node function.
    """
    def phase3b(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=3, action="phase_start", step="3b")
        # Phase 3b is a pass-through guard in the Hybrid graph: the slice-level
        # collision detector (find_slice_ambiguities) is not part of this
        # package's copied surface, so no slice-level clarification is raised
        # here. The Phase 2 term guard and the Phase 5 ontology gate remain the
        # active disambiguation/grounding backstops.
        _emit_phase(ctx, phase=3, action="phase_result", step="3b",
                    ambiguous=False)
    return phase3b


def _make_phase4(ctx: WorkflowContext,
                 deps: HybridDeps) -> Callable[[WorkflowContext], None]:
    """Build the Phase 4 node — NL -> SQL generation (negative-constraint aware).

    On a Phase 5 regenerate back-edge this consumes ``ctx.grounding_feedback`` as
    the generator's ``negative_constraint`` and CLEARS ``ctx.grounding_missing``
    so the P5 -> P4 edge condition flips (otherwise the graph would loop back to
    Phase 4 forever without ever re-running Phase 5).

    Args:
        ctx: The shared workflow context.
        deps: Injected phase implementations.

    Returns:
        The Phase 4 node function.
    """
    def phase4(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=4, action="phase_start")
        # Clear the grounding back-edge flag so the Phase 5 re-check after this
        # (re)generation reflects the new SQL, not the prior round's verdict.
        regenerated = bool(ctx.grounding_missing)
        ctx.grounding_missing = []
        ctx.sql = deps.generator.generate(
            slice_text=ctx.slice_text, question=ctx.question,
            negative_constraint=ctx.grounding_feedback,
        )
        delta = dict(getattr(deps.generator, "last_usage", {}) or {})
        add_usage(ctx, delta)
        _emit_phase(ctx, phase=4, action="phase_result",
                    regenerated=regenerated, sql=ctx.sql)
    return phase4


def _make_phase5(ctx: WorkflowContext,
                 deps: HybridDeps) -> Callable[[WorkflowContext], None]:
    """Build the Phase 5 node — ontology-verified grounding gate + execution.

    In order, mutating the shared ctx:

    1. Fetch the thin-VKG structural slice for the candidates. A fetch failure
       sets ``degraded = "hybrid_verify_unavailable"`` and returns (fail loud —
       never a silent single-engine answer).
    2. Build the inverse Ontop mapping index, bridge the SQL into ontology
       triples, and verify each triple against its predicate's ``rdfs:domain``.
    3. Grounded (no misses) -> run execution, clear ``grounding_missing``,
       terminal.
    4. Missing -> increment ``grounding_rounds``. Over the ceiling abstains
       (``grounding_unresolved``); otherwise records the misses + a negative
       constraint string and leaves the P5 -> P4 back-edge to regenerate.

    Args:
        ctx: The shared workflow context.
        deps: Injected phase implementations.

    Returns:
        The Phase 5 node function.
    """
    def phase5(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=5, action="phase_start")
        # Step 1 — fetch the thin-VKG structural slice. Fail loud on any error:
        # verification is unavailable, which must surface as a hard degrade
        # rather than a silently weaker (RAG-only) answer.
        try:
            slice_graph = deps.fetch.get(
                namespace=ctx.namespace, candidates=ctx.candidates,
            )
        except Exception as exc:  # noqa: BLE001 — fail loud, don't fall through
            ctx.degraded = "hybrid_verify_unavailable"
            ctx.degraded_detail = str(exc)
            logger.warning("phase5.fetch_failed: %s", exc)
            _emit_phase(ctx, phase=5, action="phase_result",
                        grounded=False, degraded=ctx.degraded)
            return

        # Steps 2-4 — bridge the SQL to ontology triples and verify grounding.
        idx = build_inverse_index(slice_graph, vocab=deps.vocab)
        bridge = sql_to_triples(ctx.sql, idx=idx, dialect=deps.dialect)
        missing = verify_sql_grounding(
            triples=bridge.triples, slice_graph=slice_graph,
        )

        # Un-VERIFIABLE SQL must NOT count as grounded — that would defeat the
        # "ontology-verify out / never a wrong answer" guarantee. Two cases the
        # empty-``missing`` check above would otherwise wave through:
        #   * parse_failed  — sqlglot couldn't parse it, so we extracted nothing.
        #   * no triples but unmapped columns present — e.g. an UNQUALIFIED
        #     column (``SELECT state_tc FROM address``) or a column with no
        #     Ontop mapping: the bridge emits no triple, so grounding is vacuous
        #     even though the query references schema we cannot verify.
        # A genuinely triple-free query with NOTHING unmapped (e.g.
        # ``SELECT COUNT(*) FROM address``) has nothing to verify and is allowed
        # through. We route the unverifiable cases into the SAME regenerate/abstain
        # path as a real miss so the generator gets a chance to qualify columns.
        if not missing and (bridge.parse_failed or bridge.unmapped):
            if bridge.parse_failed:
                missing = ["unverifiable:sql_parse_failed"]
            else:
                missing = [f"unverifiable:unmapped:{c}" for c in bridge.unmapped]

        if not missing:
            # Grounded — clear any prior miss and run the bounded execution agent.
            ctx.grounding_missing = []
            ctx.execution_result = deps.run_execution(ctx) or {}
            delta = dict(ctx.execution_result.get("usage", {}) or {})
            add_usage(ctx, delta)
            _emit_phase(ctx, phase=5, action="phase_result", grounded=True,
                        rowCount=len(ctx.execution_result.get("rows", [])))
            return

        # Not grounded — either loop back to Phase 4 or abstain at the ceiling.
        ctx.grounding_rounds += 1
        if ctx.grounding_rounds > MAX_GROUNDING_ROUNDS:
            ctx.degraded = "grounding_unresolved"
            _emit_phase(ctx, phase=5, action="phase_result",
                        grounded=False, groundingRound=ctx.grounding_rounds,
                        degraded=ctx.degraded, missing=missing)
            return
        # Regenerate: record the misses and thread them in as a negative
        # constraint so Phase 4 avoids the flagged predicate(s) on the next pass.
        ctx.grounding_missing = missing
        ctx.grounding_feedback = "; ".join(missing)
        _emit_phase(ctx, phase=5, action="phase_result",
                    grounded=False, groundingRound=ctx.grounding_rounds,
                    missing=missing)
    return phase5


# ---------------------------------------------------------------------------
# Graph assembly + entry point (Hybrid edges).
# ---------------------------------------------------------------------------
def build_hybrid_graph(ctx: WorkflowContext, deps: HybridDeps) -> Any:
    """Build the Strands Graph wiring the 6 phase nodes + 2 terminal nodes.

    Conditional edges and node functions both close over the shared ``ctx`` so
    routing flags (``degraded`` / ``needs_clarification`` / ``grounding_missing``)
    set by a node steer the next hop. Terminal nodes (clarification / degraded)
    are no-op sinks — the response is built from ``ctx`` after the run.

    The Phase 5 back-edge always targets Phase 4 (regenerate SQL): a grounding
    miss means the model used a predicate/column that isn't ontology-grounded on
    its subject class, which widening the slice cannot fix.

    Args:
        ctx: The shared workflow context.
        deps: Injected phase implementations.

    Returns:
        The built Strands ``Graph``.
    """
    def _emit_terminal_answer(_c: WorkflowContext) -> None:
        """Emit the eval final-answer span at a clarify / degraded terminal.

        Must run HERE, inside the terminal node, while the graph's multiagent
        span is still the active (recording) OTEL context — that is the only
        position the SESSION harvester treats as the conversation's final answer.
        A post-graph emit orphans into a separate trace the harvester ignores.
        Not needed on the grounded-success path: there the bounded answer
        renderer inside ``deps.run_execution`` makes a real model call, so the SDK
        already emits an in-graph span carrying the NL answer. Fail-soft: the
        emitter swallows its own errors, and a missing emitter is a no-op.
        """
        if deps.answer_emitter is not None:
            deps.answer_emitter(ctx)

    gb = GraphBuilder()
    gb.add_node(_FnNode(name="phase1", fn=_make_phase1(ctx, deps), ctx=ctx), "phase1")
    gb.add_node(_FnNode(name="phase2", fn=_make_phase2(ctx, deps), ctx=ctx), "phase2")
    gb.add_node(_FnNode(name="phase3", fn=_make_phase3(ctx, deps), ctx=ctx), "phase3")
    gb.add_node(_FnNode(name="phase3b", fn=_make_phase3b(ctx, deps), ctx=ctx), "phase3b")
    gb.add_node(_FnNode(name="phase4", fn=_make_phase4(ctx, deps), ctx=ctx), "phase4")
    gb.add_node(_FnNode(name="phase5", fn=_make_phase5(ctx, deps), ctx=ctx), "phase5")
    gb.add_node(_FnNode(name="clarify", fn=_emit_terminal_answer, ctx=ctx), "clarify")
    gb.add_node(_FnNode(name="degraded", fn=_emit_terminal_answer, ctx=ctx), "degraded")

    gb.set_entry_point("phase1")

    # Phase 1 -> Phase 2 (candidates) | degraded (empty)
    gb.add_edge("phase1", "phase2", condition=lambda s: ctx.degraded != "phase1_empty")
    gb.add_edge("phase1", "degraded", condition=lambda s: ctx.degraded == "phase1_empty")

    # Phase 2 -> Phase 3 (clear) | clarify (ambiguous)
    gb.add_edge("phase2", "phase3", condition=lambda s: ctx.needs_clarification is None)
    gb.add_edge("phase2", "clarify", condition=lambda s: ctx.needs_clarification is not None)

    # Phase 3 -> Phase 3b (slice sufficient) | degraded (judge never reached
    # sufficiency within MAX_PHASE3_ROUNDS).
    gb.add_edge("phase3", "phase3b", condition=lambda s: ctx.degraded != "phase3_max_rounds")
    gb.add_edge("phase3", "degraded", condition=lambda s: ctx.degraded == "phase3_max_rounds")

    # Phase 3b -> Phase 4 (clear) | clarify (ambiguous)
    gb.add_edge("phase3b", "phase4", condition=lambda s: ctx.needs_clarification is None)
    gb.add_edge("phase3b", "clarify", condition=lambda s: ctx.needs_clarification is not None)

    # Phase 4 -> Phase 5 (always: generation has no separate syntax-degrade here)
    gb.add_edge("phase4", "phase5")

    # Phase 5 -> Phase 4 (ungrounded back-edge: regenerate SQL w/ negative
    #                     constraint) | degraded (abstain / verify unavailable)
    #                     | terminal (grounded — no out-edge needed).
    gb.add_edge("phase5", "phase4",
                condition=lambda s: bool(ctx.grounding_missing)
                and ctx.degraded is None)
    gb.add_edge("phase5", "degraded",
                condition=lambda s: ctx.degraded in ("grounding_unresolved",
                                                     "hybrid_verify_unavailable"))

    gb.reset_on_revisit(True)
    gb.set_max_node_executions(MAX_NODE_EXECUTIONS)
    return gb.build()


def run_hybrid_workflow(*, question: str, namespace: str, deps: HybridDeps,
                        phase_sink: Optional[Callable[[Optional[int], str, Dict[str, Any]], None]] = None,
                        clarification_resolution: Optional[Any] = None,
                        catalog_id: str = "",
                        ) -> WorkflowContext:
    """Run the Tier 2 Hybrid resolution graph and return the populated context.

    Mirrors the shared ``run_tier2_graph`` boilerplate: create the context, build
    the graph over it, invoke it synchronously (nodes mutate the context in
    place), and return the context.

    Args:
        question: The natural-language user question.
        namespace: Semantic-layer namespace for KB / Neptune scoping.
        deps: Injected phase implementations
            (router / builder / generator / fetch / run_execution / vocab / dialect).
        phase_sink: Optional live per-phase trace sink (streaming path).
        clarification_resolution: Resolution object when this turn answers a prior
            clarification; ``None`` on a normal turn.
        catalog_id: The Athena catalog the layer's tables live in, from the
            layer's metadata config. Phase 1 can only recover the DATABASE from a
            ``database.table`` candidate id, so the catalog must be supplied here
            — without it Athena resolves the schema against the default Glue
            catalog and an S3 Tables database fails ``SCHEMA_NOT_FOUND``.

    Returns:
        The :class:`WorkflowContext` after the graph completes — carries the
        slice, SQL, execution result, clarification payload, and degraded flag.
    """
    ctx = WorkflowContext(
        question=question, namespace=namespace,
        catalog_id=catalog_id,
        phase_sink=phase_sink,
        clarification_resolution=clarification_resolution,
    )
    graph = build_hybrid_graph(ctx, deps)
    graph(ctx.question)  # synchronous invoke; nodes mutate ctx in place
    return ctx
