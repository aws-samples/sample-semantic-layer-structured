"""Mode-agnostic Tier 2 graph primitives shared by both query agents.

The metadata_query_agent (RAG/SQL) and ontology_query_agent (VKG/SPARQL) both
resolve a question with the same Strands multi-agent ``Graph`` shape:

    Phase 1  topic router        (candidate tables / class+property IRIs)
    Phase 2  term disambiguation (ambiguous term → clarification)
    Phase 3  slice builder + judge loop
    Phase 3b slice disambiguation (slice-level collisions → clarification)
    Phase 4  query generate + validate (SQL: sqlglot / SPARQL: rdflib)
    Phase 5  grounding gate + bounded execution agent

The engine, the deterministic-phase node adapter, per-phase tracing, token
accounting, and the shared mutable :class:`WorkflowContext` live here. Each
agent supplies its own mode-specific phase-function factories and — crucially —
its own *edge assembly*, because the Phase 5 grounding back-edge is NOT
mode-agnostic:

    * RAG loops a grounding failure straight back to Phase 4 (regenerate SQL):
      a hallucinated column can't be conjured by widening the slice.
    * VKG uses a *hybrid* back-edge: a real-but-out-of-slice predicate loops to
      Phase 3 (expand), a hallucinated IRI loops to Phase 4 (regenerate).

So the graph topology is assembled per-agent from these shared nodes (see each
agent's ``tier2/workflow.py``), not parameterized here.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from strands.multiagent.base import MultiAgentBase, MultiAgentResult, Status

logger = logging.getLogger(__name__)


# Loop ceilings — keep the graph from spinning. Phase 3's judge loop and the
# Phase 5 grounding back-edge are each bounded independently, and the graph
# carries a global node-execution backstop on top. Shared by both agents.
MAX_PHASE3_ROUNDS = 3
MAX_GROUNDING_ROUNDS = 2
MAX_NODE_EXECUTIONS = 40
# Raised 12000 -> 20000 (2026-06-26): richer per-table/column descriptions from the
# de-layering enrichment brief (join/derivation/label recipes now live in the layer,
# not the generator prompt) made a multi-table slice + its bridge tables overflow the
# old 12000 budget. _fit() then evicted/truncated below what the Phase-3 judge needed
# and false-rejected EXISTING columns (e.g. party.party_type), degrading answerable
# questions with phase3_max_rounds. 20000 fits the enriched slice with its bridges.
SLICE_TOKEN_BUDGET = 20000


# ---------------------------------------------------------------------------
# Shared workflow state
# ---------------------------------------------------------------------------
@dataclass
class WorkflowContext:
    """Mutable state threaded through every phase of one Tier 2 resolution.

    A single instance is created per ``tier2_*_workflow`` call; the phase node
    functions and the conditional-edge predicates all close over it.

    The generated query is stored on the canonical ``sql`` field; both agents
    read/write it through the mode-appropriate accessor — RAG via ``.sql`` and
    VKG via ``.sparql_query`` (both alias the same underlying field) — so the
    response payload each agent builds stays byte-compatible with its existing
    ``sql_query`` / ``sparql_query`` shape. ``.query`` is a mode-neutral alias.

    Attributes:
        question: The natural-language user question.
        namespace: Semantic-layer namespace used for KB / Neptune scoping.
        kb_id: Bedrock Knowledge Base id (RAG Phase 1 retrieval); ``""`` for VKG.
        database_name: Athena database (extracted from Phase 1 metadata).
        catalog_id: Athena catalog (extracted from Phase 1 metadata).
        candidates: Phase 1 candidate ids — ``database.table`` (RAG) or class /
            property IRIs (VKG), ranked by score.
        slice_text: Serialized schema/ontology slice (Phase 3 output) — JSON
            (RAG) or Turtle (VKG).
        disambiguation: Resolved term bindings recorded by Phase 2/3b for the
            Phase 4 generator prompt.
        needs_clarification: Clarification payload when Phase 2 or 3b cannot
            resolve ambiguity heuristically; ``None`` otherwise.
        clarification_source: ``"phase2"`` | ``"phase3b"`` (diagnostic only).
        clarification_resolution: A
            :class:`agents.shared.clarification.ClarificationResolution` when
            this turn answers a prior clarification — Phase 1 prunes the rival
            candidates it names so the now-unambiguous term resolves cleanly.
            ``None`` on a normal turn.
        sql: Generated + syntax-validated query (Phase 4 output). SQL for RAG,
            SPARQL for VKG (see ``.sparql_query`` accessor).
        phase3_rounds: Number of Phase 3 build/expand iterations executed.
        degraded: Non-None when a phase exited via a degraded path
            (``"phase1_empty"``, ``"phase3_max_rounds"``, ``"sql_repair_failed"`` /
            ``"sparql_repair_failed"``, ``"grounding_unresolved"``).
        execution_result: Phase 5 execution result dict (columns/rows/flags,
            plus ``answer``/``usage`` and — VKG only — ``n_quads``).
        grounding_rounds: Number of Phase 5 grounding loop-backs taken.
        grounding_missing: Identifiers the grounding gate found absent from the
            slice that route to a Phase 3 *expand* (VKG out-of-slice case).
        grounding_feedback: Negative-constraint string the grounding gate hands
            the generator on a Phase 4 *regenerate* back-edge.
        kb_sources: Source citations (RAG KB chunks / VKG n_quads citations).
        usage: Running ``{inputTokens, outputTokens, totalTokens}`` total.
        phase_sink: Live per-phase trace sink ``(phase, action, payload) ->
            None``; ``None`` disables tracing (single-shot path / tests).
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
    # Set by the agent's _run_query when this turn answers a prior clarification.
    # Typed as Any to avoid a hard import of the clarification module here (kept
    # mode-agnostic / dependency-light). See apply_clarification_resolution.
    clarification_resolution: Optional[Any] = None
    # Options ({id, label}) + ambiguous term(s) the user was shown on the PRIOR
    # turn's clarification, passed in ONLY when this turn re-asks the SAME
    # question (the agent gates on original_question equality). Phase 2 reuses
    # these for a no-specific-term (low-confidence) re-ask instead of re-deriving
    # the option list from a fresh, non-deterministic retrieval — otherwise the
    # user sees a different candidate set every turn and can never converge.
    # Empty on a first clarification or a brand-new question.
    prior_clarification_options: List[Dict[str, Any]] = field(default_factory=list)
    prior_clarification_terms: List[str] = field(default_factory=list)
    sql: str = ""
    phase3_rounds: int = 0
    degraded: Optional[str] = None
    # Optional human-readable detail for a degraded terminal — set alongside
    # ``degraded`` when the reason carries a specific user-facing message (e.g. the
    # Phase 3b ``relationship_unsupported`` guard). ``None`` for generic reasons that
    # map to a fixed message in the response builder.
    degraded_detail: Optional[str] = None
    execution_result: Dict[str, Any] = field(default_factory=dict)
    grounding_rounds: int = 0
    grounding_missing: List[str] = field(default_factory=list)
    grounding_feedback: str = ""
    # VKG-only hybrid back-edge selector — ``"expand"`` (real-but-out-of-slice
    # IRI → Phase 3) or ``"regenerate"`` (hallucinated/misused IRI → Phase 4),
    # set by the VKG Phase 5 grounding gate and cleared by Phase 3 / Phase 4.
    # RAG never sets it (its back-edge always targets Phase 4), so it stays
    # ``None`` and the RAG edge conditions are unaffected.
    grounding_route: Optional[str] = None
    kb_sources: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, int] = field(
        default_factory=lambda: {"inputTokens": 0, "outputTokens": 0,
                                 "totalTokens": 0})
    phase_sink: Optional[Callable[[Optional[int], str, Dict[str, Any]], None]] = None
    # Prior turns of this chat session (``[{role, content}]``, oldest first),
    # set by the agent entrypoint. Read ONLY by the eval ``answer_emitter`` so the
    # final-answer span carries the multi-turn trajectory the SESSION judges score.
    # Empty on a single-turn / non-chat invocation. Never affects resolution.
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)

    # Mode-neutral / SPARQL accessors over the canonical ``sql`` field. These
    # keep VKG code readable (``ctx.sparql_query``) without forcing the RAG
    # agent (and its tests) off the ``sql`` field name they already use.
    @property
    def query(self) -> str:
        """Mode-neutral alias for the generated query (SQL or SPARQL)."""
        return self.sql

    @query.setter
    def query(self, value: str) -> None:
        self.sql = value

    @property
    def sparql_query(self) -> str:
        """VKG-facing alias for the generated SPARQL query."""
        return self.sql

    @sparql_query.setter
    def sparql_query(self, value: str) -> None:
        self.sql = value


def _emit_phase(ctx: WorkflowContext, *, phase: Optional[int], action: str,
                **payload: Any) -> None:
    """Forward a per-phase trace event to the context's sink (fail-soft).

    Args:
        ctx: The active workflow context.
        phase: Phase number (1-5) or ``None``.
        action: ``"phase_start"`` or ``"phase_result"``.
        **payload: Extra fields merged into the event (e.g. ``status``,
            ``round``, ``candidateCount``).
    """
    sink = ctx.phase_sink
    if sink is None:
        return
    try:
        sink(phase, action, dict(payload))
    except Exception as exc:  # noqa: BLE001 — tracing must never break a query
        logger.debug("phase_sink failed (non-fatal) phase=%s action=%s: %s",
                     phase, action, exc)


def apply_clarification_resolution(ctx: WorkflowContext) -> None:
    """Prune Phase 1 rival candidates named by a resolved prior clarification.

    Runs at the END of Phase 1 in both agents (after candidates are produced,
    before Phase 2). When this turn answers a prior clarification (the agent set
    ``ctx.clarification_resolution``), drop every candidate whose local name is
    a *rival* the user did NOT choose. With the rivals gone, Phase 2's term→table
    (or term→IRI) map sees the disambiguated term owning a single candidate, so
    it resolves CLEAR instead of re-firing the identical clarification.

    Mode-agnostic: candidate ids are normalised via
    :func:`agents.shared.clarification.local_name`, which collapses both
    ``database.table`` (RAG) and ``…/ClassName`` IRIs (VKG) to a bare lower-cased
    name — the same shape the option ids carry.

    Fail-soft: a no-op when there is no resolution, no candidates, or when
    pruning would empty the candidate list (we never strand the query with zero
    tables — better to clarify again than to fail).

    Args:
        ctx: The active workflow context (mutated in place).
    """
    resolution = ctx.clarification_resolution
    if resolution is None or not ctx.candidates:
        return
    rival_names = set(getattr(resolution, "rival_names", []) or [])
    chosen_names = set(getattr(resolution, "chosen_names", []) or [])
    if not rival_names and not chosen_names:
        return
    # Import here (not at module top) to keep this module dependency-light and
    # avoid any import cycle with the clarification helper.
    try:
        from agents.shared.clarification import local_name
    except ImportError:  # container path: agents/ is on PYTHONPATH
        from shared.clarification import local_name  # type: ignore

    # Prune rivals the user did NOT choose (no-op when no rivals were offered).
    if rival_names:
        kept = [
            c for c in ctx.candidates
            if local_name(c) in chosen_names or local_name(c) not in rival_names
        ]
        if kept and len(kept) < len(ctx.candidates):
            logger.info("clarification: pruned candidates %s -> %s (chose %s)",
                        ctx.candidates, kept, chosen_names)
            ctx.candidates = kept

    # SEED a chosen table that retrieval never surfaced. Pruning alone cannot
    # answer a clarified query when the re-run question is the bare original
    # (e.g. "How many are there?" → user picks "party"): that text has no noun,
    # so Phase 1 KB retrieval returns party-unrelated tables and `party` is
    # simply absent from `candidates`. Without seeding, the slice is built from
    # the wrong tables and the query degrades — even though the SAME count
    # ("How many parties are there?") succeeds when asked directly. So if a
    # chosen name is missing from the (post-prune) candidate set, add it. The
    # chosen option id is a BARE table name; reconstruct the full `db.table` id
    # from a sibling candidate's database (all tables in a layer share it) or the
    # namespace, then prepend it so it leads the relevance order.
    present = {local_name(c) for c in ctx.candidates}
    # Seed from the RAW chosen option ids (this turn + prior) so a VKG option id
    # that is a full class IRI is reconstructed correctly. ``chosen_names`` is
    # lower-cased + local-only and loses the IRI/casing needed to fetch the class.
    chosen_ids = list(getattr(resolution, "chosen_ids", []) or [])
    for p in getattr(resolution, "prior", []) or []:
        cid = getattr(p, "chosen_id", "")
        if cid:
            chosen_ids.append(cid)
    missing_ids = [cid for cid in chosen_ids if cid and local_name(cid) not in present]
    if missing_ids:
        seeded: List[str] = []
        for cid in missing_ids:
            # An IRI option id (VKG: contains "://" or a path "/") is already a
            # fetchable class id — seed it verbatim (full IRI, correct casing).
            if "://" in cid or "/" in cid:
                seeded.append(cid)
                continue
            # RAG: the option id is a bare table name — reconstruct "db.table"
            # from a sibling candidate's database (all tables share it) so the
            # slice builder targets the right schema.
            db = ""
            for c in ctx.candidates:
                if "." in c and "://" not in c:
                    db = c.split(".", 1)[0]
                    break
            if not db and ctx.namespace and "." in ctx.namespace:
                db = ctx.namespace.split(".", 1)[0]
            seeded.append(f"{db}.{cid}" if db else cid)
        logger.info("clarification: seeding chosen candidate(s) absent from "
                    "retrieval: %s", seeded)
        # Lead with the seeded chosen candidates so they rank first downstream.
        ctx.candidates = list(dict.fromkeys(seeded + ctx.candidates))


# The token-usage keys we track. Bedrock Converse with prompt caching
# (cache_config=auto on the query model) reports the cache components SEPARATELY
# from inputTokens, while ``totalTokens`` is the cache-INCLUSIVE grand total:
#   totalTokens = inputTokens + outputTokens + cacheReadInputTokens + cacheWriteInputTokens
# So a footer that shows only "totalTokens (inputTokens in / outputTokens out)"
# looks wrong — the cache-read portion is in the total but missing from the
# in/out breakdown (the "numbers don't add up" report). We surface the cache
# fields so the breakdown reconciles.
_USAGE_KEYS = (
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "cacheReadInputTokens",
    "cacheWriteInputTokens",
)


def extract_usage(agent_result: Any) -> Dict[str, int]:
    """Return the per-call token usage from a Strands result.

    Reads ``result.metrics.accumulated_usage`` (dict or attr form) and returns
    ``{inputTokens, outputTokens, totalTokens, cacheReadInputTokens,
    cacheWriteInputTokens}``. Cache fields are captured (not just the base trio)
    so the running total reconciles with Bedrock's cache-inclusive
    ``totalTokens``. Returns zeros when metrics are unavailable so callers can
    always add the dict.

    Args:
        agent_result: A Strands ``AgentResult`` (or anything without metrics).
    """
    out = {k: 0 for k in _USAGE_KEYS}
    try:
        usage = agent_result.metrics.accumulated_usage
    except AttributeError:
        return out
    for key in _USAGE_KEYS:
        value = (usage.get(key) if isinstance(usage, dict)
                 else getattr(usage, key, None))
        if value is not None:
            out[key] = int(value)
    return out


def add_usage(ctx: WorkflowContext, delta: Dict[str, int]) -> None:
    """Accumulate a per-call usage ``delta`` into the context's running total.

    Sums all usage keys (incl. the cache components) so the running total stays
    consistent with each call's cache-inclusive ``totalTokens``.
    """
    for key in _USAGE_KEYS:
        if key in delta or key in ctx.usage:
            ctx.usage[key] = ctx.usage.get(key, 0) + int(delta.get(key, 0) or 0)


# ---------------------------------------------------------------------------
# Deterministic-phase node adapter
# ---------------------------------------------------------------------------
class _FnNode(MultiAgentBase):
    """Wrap a deterministic ``(ctx) -> None`` phase function as a Graph node.

    The phase function mutates the shared :class:`WorkflowContext` in place
    (no return threading). The blocking body is run via ``asyncio.to_thread``
    so boto3 / sqlglot / rdflib / Bedrock calls don't stall the graph's event
    loop.
    """

    def __init__(self, *, name: str, fn: Callable[[WorkflowContext], None],
                 ctx: WorkflowContext) -> None:
        """Construct the node.

        Args:
            name: Node id (also used in logs).
            fn: The phase function to run; receives the shared context.
            ctx: The shared workflow context instance.
        """
        super().__init__()
        self._name = name
        self._fn = fn
        self._ctx = ctx

    async def invoke_async(self, task: Any, invocation_state: Optional[Dict[str, Any]] = None,
                           **kwargs: Any) -> MultiAgentResult:
        """Run the phase function in a worker thread; return a COMPLETED result."""
        await asyncio.to_thread(self._fn, self._ctx)
        return MultiAgentResult(status=Status.COMPLETED)


# ---------------------------------------------------------------------------
# Injected phase dependencies (mode-agnostic shape)
# ---------------------------------------------------------------------------
@dataclass
class PhaseDeps:
    """Injected implementations the phase functions call into.

    The shape is shared by both agents; only the concrete implementations and
    the ``run_execution`` call signature differ (RAG: ``(sql, db, catalog)``;
    VKG: a closure that runs SPARQL on Neptune and maps results to n_quads).
    Because each agent supplies its own Phase 5 factory, only that agent's own
    code calls ``run_execution`` — so the differing signature is safe.

    Attributes:
        router: Phase 1 topic router (find_candidates + last_structured).
        builder: Phase 3 slice builder (build / is_sufficient / expand).
        generator: Phase 4 query generator (generate + last_usage).
        run_execution: Callable that runs the bounded execution agent and
            returns the parsed result dict (columns/rows/flags/answer/usage).
        recall_resolver: Optional ``(term, candidate_ids) -> Optional[str]`` that
            consults the user's long-term AgentCore Memory lessons and returns the
            single candidate id a prior session tied ``term`` to. Phase 2 calls it
            to silently resolve an ambiguous term from memory before surfacing a
            user clarification. ``None`` (the default) disables recall — e.g. when
            ``LESSONS_MEMORY_ID`` is unset or the user/layer is unknown.
        answer_emitter: Optional ``(ctx) -> None`` eval-telemetry hook called from
            the graph's TERMINAL nodes (clarify / degraded / grounded success) so
            the final-answer span is emitted while the graph's multiagent span is
            still the active (recording) OTEL context — the only position the
            SESSION harvester treats as the conversation's final answer. WHY here
            and not after the graph returns: by then the graph span has ended, so
            a post-graph emit orphans into a separate trace the harvester ignores
            (it then grades the last in-graph model span — for VKG that is the
            Phase-4 SPARQL generator, since VKG Phase 5 is deterministic and emits
            no answer-like LLM span, unlike the metadata agent's bounded execution
            agent). Mirrors ``emit_grounding_span``, which lands for the same
            reason. ``None`` (the default) disables in-graph answer telemetry.
    """

    router: Any
    builder: Any
    generator: Any
    run_execution: Callable[..., Dict[str, Any]]
    recall_resolver: Optional[Callable[..., Optional[str]]] = None
    answer_emitter: Optional[Callable[[Any], None]] = None


# ---------------------------------------------------------------------------
# Generic runner
# ---------------------------------------------------------------------------
def run_tier2_graph(*, ctx: WorkflowContext,
                    build_graph: Callable[[WorkflowContext], Any]
                    ) -> WorkflowContext:
    """Build the graph via ``build_graph(ctx)`` and synchronously invoke it.

    Both agents share the "create ctx → build graph → invoke → return ctx"
    boilerplate; the agent-specific edge assembly lives behind ``build_graph``.

    Args:
        ctx: The shared workflow context (already populated with question /
            namespace / phase_sink).
        build_graph: Callable that returns a built Strands Graph wired over
            ``ctx``. Invoked once.

    Returns:
        The same :class:`WorkflowContext`, mutated in place by the graph nodes.
    """
    graph = build_graph(ctx)
    graph(ctx.question)  # synchronous invoke; nodes mutate ctx in place
    return ctx
