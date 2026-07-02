"""Tier 2 RAG resolution as a Strands multi-agent Graph workflow.

Implements the Tier 2 resolution as a single linear Strands graph:

    Phase 1  topic router          (KB retrieval → candidate tables)
    Phase 2  term disambiguation   (ambiguous term → clarification)
    Phase 3  slice builder + judge loop
    Phase 3b slice disambiguation  (slice-level collisions → clarification)
    Phase 4  SQL generate + validate (sqlglot parse + 1 repair)
    Phase 5  grounding gate + bounded execution agent
               - gate: every table/column/join/literal must be in the slice;
                 if not, loop back to Phase 4 (regenerate w/ feedback)
               - execute: run SQL on Athena, fix errors, recheck 0-rows,
                 LIMIT 100 + over-limit flag

The mode-agnostic primitives — :class:`WorkflowContext`, :class:`PhaseDeps`,
the :class:`_FnNode` adapter, :func:`_emit_phase`, the usage helpers, and the
loop ceilings — now live in :mod:`agents.shared.tier2_graph` and are shared
with the ontology (VKG) agent. This module provides only the RAG-specific
phase-function factories and the RAG edge assembly (Phase 5 loops back to
Phase 4 — there is no Phase-3 expand back-edge in RAG: a hallucinated column
can't be conjured by widening the slice).

The deterministic phases (1, 3, 3b, 4, and the grounding gate half of 5) are
not LLM agents, so they ride on the ``_FnNode`` adapter. All phases read and
mutate a single shared ``WorkflowContext`` instance that node functions and
conditional-edge predicates both close over — Strands' ``GraphState`` does not
expose ``invocation_state`` to edge conditions, so the shared-closure pattern
is how data and routing flags flow between phases.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from strands.multiagent.graph import GraphBuilder

# Mode-agnostic primitives shared with the ontology (VKG) agent. Dual-import:
# repo-root imports use ``agents.shared``; the container has ``shared`` on
# PYTHONPATH directly (no top-level ``agents`` package).
try:
    from agents.shared.tier2_graph import (
        MAX_GROUNDING_ROUNDS,
        MAX_NODE_EXECUTIONS,
        MAX_PHASE3_ROUNDS,
        SLICE_TOKEN_BUDGET,
        PhaseDeps,
        WorkflowContext,
        apply_clarification_resolution,
        _emit_phase,
        _FnNode,
        add_usage,
        extract_usage,
        run_tier2_graph,
    )
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.tier2_graph import (  # type: ignore
        MAX_GROUNDING_ROUNDS,
        MAX_NODE_EXECUTIONS,
        MAX_PHASE3_ROUNDS,
        SLICE_TOKEN_BUDGET,
        PhaseDeps,
        WorkflowContext,
        apply_clarification_resolution,
        _emit_phase,
        _FnNode,
        add_usage,
        extract_usage,
        run_tier2_graph,
    )

logger = logging.getLogger(__name__)


def _slice_presence_index(slice_text: str) -> Tuple[set, set]:
    """Parse a serialized RAG slice into ``(table_names, qualified_columns)``.

    Returns two lower-cased sets used by the judge self-contradiction override:

      * ``table_names`` — every BARE table name in the slice's ``tables`` array
        (the last dot-segment, so both ``normalized.party`` and a hypothetical
        ``party`` collapse to ``party``). The judge must NOT require a particular
        schema qualifier, so we compare on the bare name.
      * ``qualified_columns`` — ``{table_bare}.{column}`` for every entry in the
        slice's ``columns`` array, so a judge ``missing`` of ``db.table.column``
        can be checked against the column that actually exists.

    A slice that fails to parse yields two empty sets (the override then never
    fires — the loop falls through to its normal degrade).

    Args:
        slice_text: The serialized JSON slice from the builder.

    Returns:
        ``(table_names, qualified_columns)`` as lower-cased string sets.
    """
    try:
        obj = json.loads(slice_text) if slice_text else {}
    except (json.JSONDecodeError, TypeError):
        return set(), set()
    if not isinstance(obj, dict):
        return set(), set()
    table_names: set = set()
    for tid in obj.get("tables", []) or []:
        if isinstance(tid, str) and tid:
            table_names.add(tid.rsplit(".", 1)[-1].strip().lower())
    qualified_columns: set = set()
    bare_columns: set = set()
    for col in obj.get("columns", []) or []:
        if not isinstance(col, dict):
            continue
        tid = (col.get("table_id") or "").rsplit(".", 1)[-1].strip().lower()
        name = (col.get("name") or "").strip().lower()
        if name:
            bare_columns.add(name)
        if tid and name:
            qualified_columns.add(f"{tid}.{name}")
    # bare_columns is folded into qualified_columns under a wildcard table sentinel
    # so _missing_entry_present can fall back to a bare-name match when the judge
    # mis-qualifies a present column with the wrong (or a fabricated) table. We keep
    # the two-set return signature stable by smuggling bare names as "*.<col>".
    for name in bare_columns:
        qualified_columns.add(f"*.{name}")
    return table_names, qualified_columns


def _missing_entry_present(entry: str, table_names: set,
                           qualified_columns: set) -> bool:
    """True when a judge ``missing`` entry is in fact present in the slice.

    Compares on table/column NAME only — never on a schema/database qualifier —
    so a judge that fabricates a layer prefix the user typed (``curated.party``
    when the slice carries ``normalized.party``) is recognised as a false
    negative. Handles both shapes the judge emits:

      * ``[db.]table``            → present iff the bare table name is in the slice.
      * ``[db.]table.column``     → present iff ``table.column`` is a real slice
        column (and, defensively, the column tail might itself be a 2-segment
        ``table.column`` request with no db, hence the suffix checks).

    Args:
        entry: One judge-reported missing identifier.
        table_names: Bare table names present in the slice.
        qualified_columns: ``{table}.{column}`` pairs present in the slice.

    Returns:
        ``True`` when the entry names something already in the slice.
    """
    cleaned = (entry or "").strip().lower()
    if not cleaned:
        return False
    parts = cleaned.split(".")
    if len(parts) >= 3:
        # db.table.column → check the table.column tail against real columns, and
        # fall back to a bare-column match (judge mis-qualified a present column
        # with a wrong/fabricated table — e.g. it asks for party.party_type when
        # party_type lives on the present party table under a different table_id).
        table, column = parts[-2], parts[-1]
        return (f"{table}.{column}" in qualified_columns
                or f"*.{column}" in qualified_columns)
    if len(parts) == 2:
        # Ambiguous: either db.table OR table.column. Accept either reading —
        # present if the bare table exists, or it is a real qualified table.column.
        # (No bare-column fallback here: a 2-part entry is more likely a db.table
        # the slice genuinely lacks than a mis-qualified column.)
        first, second = parts[0], parts[1]
        return second in table_names or f"{first}.{second}" in qualified_columns
    # Single bare token: a table name (the common case). A bare column is rare here
    # and risky to accept (many tables share `id`/`status`), so keep it table-only.
    return cleaned in table_names


# Public names callers (main.py, tests) import from this module.
__all__ = [
    "MAX_GROUNDING_ROUNDS",
    "MAX_NODE_EXECUTIONS",
    "MAX_PHASE3_ROUNDS",
    "SLICE_TOKEN_BUDGET",
    "PhaseDeps",
    "WorkflowContext",
    "add_usage",
    "extract_usage",
    "build_tier2_graph",
    "tier2_rag_workflow",
]


# ---------------------------------------------------------------------------
# Phase node functions — built as closures over injected deps + shared ctx
# ---------------------------------------------------------------------------
def _make_phase1(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 1 — topic router: KB retrieval → ranked candidate tables."""
    def phase1(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=1, action="phase_start")
        ctx.candidates = deps.router.find_candidates(
            question=ctx.question, namespace=ctx.namespace,
        )
        # Pull Athena execution context (database/catalog) from the Phase 1
        # structured candidates so Phase 5 needs no second KB call. The catalog
        # is REQUIRED for federated catalogs (S3 Tables) — without it execution
        # hits SCHEMA_NOT_FOUND against the default AwsDataCatalog. Prefer the
        # candidate that owns the top-ranked table.
        structured = getattr(deps.router, "last_structured", {}) or {}
        for cand in structured.get("candidates", []):
            if not isinstance(cand, dict):
                continue
            if not ctx.database_name:
                tid = cand.get("table_id", "")
                ctx.database_name = (cand.get("database_name")
                                     or (tid.split(".", 1)[0] if "." in tid else ""))
            if not ctx.catalog_id and cand.get("catalog_id"):
                ctx.catalog_id = cand["catalog_id"]
            if ctx.database_name and ctx.catalog_id:
                break
        # Build KB source citations from the structured payload in the n_quads
        # shape the UI expects (rendered as the Knowledge Base Sources panel).
        chunks_by_table = structured.get("chunks_by_table", {}) or {}
        score_by_table = {
            c.get("table_id"): c.get("score", 0.0)
            for c in structured.get("candidates", [])
        }
        sources: List[Dict[str, Any]] = []
        for tid, body in chunks_by_table.items():
            database, table = (tid.split(".", 1) if "." in tid else ("", tid))
            content = body or ""
            sources.append({
                "sourceUri": tid,
                "content": content,
                "excerpt": content[:200].strip(),
                "score": round(float(score_by_table.get(tid, 0.0)), 4),
                "tableName": table,
                "database": database,
            })
        ctx.kb_sources = sources
        # If this turn answers a prior clarification, drop the rival candidates
        # the user did not choose so Phase 2 sees the disambiguated term owning a
        # single table (no-op on a normal turn). MUST run before the empty check
        # and before building candidates_detail so the trace shows the pruned set.
        apply_clarification_resolution(ctx)
        if not ctx.candidates:
            ctx.degraded = "phase1_empty"
        # Carry the ranked candidate detail so the UI can expand the Phase 1
        # chip into the actual tables + relevance scores (not just a count).
        candidates_detail = [
            {"table": (tid.split(".", 1)[1] if "." in tid else tid),
             "database": (tid.split(".", 1)[0] if "." in tid else ""),
             "score": round(float(score_by_table.get(tid, 0.0)), 4)}
            for tid in ctx.candidates
        ]
        _emit_phase(ctx, phase=1, action="phase_result",
                    candidateCount=len(ctx.candidates),
                    candidates=candidates_detail, candidateKind="table",
                    degraded=ctx.degraded)
    return phase1


def _make_phase2(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 2 — term-level disambiguation over the Phase 1 structured payload."""
    try:
        from agents.shared.disambiguation_common import (
            build_clarification,
            build_clarification_from_options,
        )
    except ImportError:  # container path: agents/ is on PYTHONPATH
        from shared.disambiguation_common import (  # type: ignore
            build_clarification,
            build_clarification_from_options,
        )

    from .disambiguation import analyze_terms

    def phase2(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=2, action="phase_start")
        structured = getattr(deps.router, "last_structured", {}) or {}
        # Honor a clarification prune: disambiguate only against the candidates
        # that survived Phase 1 (ctx.candidates), NOT the raw router payload —
        # which still contains the pruned rival and would re-fire the identical
        # clarification (the RAG Phase 2 loop). No-op on a normal turn, where
        # ``kept`` equals every candidate. VKG Phase 2 already reads
        # ctx.candidates directly, so only RAG needs this seam.
        kept = set(ctx.candidates)
        if kept:
            structured = {
                **structured,
                "candidates": [c for c in structured.get("candidates", [])
                               if c.get("table_id") in kept],
            }
        # Carry the user's already-chosen option names (if this turn answers a
        # prior clarification) so Phase 2 treats the pick as a confident binding
        # and won't re-clarify a table the user confirmed. Phase 1 already
        # pruned the rivals; this closes the LOW_CONFIDENCE case the prune
        # cannot (picking never raises a table's cosine score above the floor).
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
                # Low-confidence / multi-domain with no specific term. If this is
                # a re-ask of the SAME question (the caller passed the prior
                # turn's options), reuse THOSE options so the user sees a stable
                # candidate set instead of a fresh, non-deterministic top-5 every
                # turn (the "different 5 each turn" churn). Otherwise derive the
                # options from this turn's top candidates.
                if ctx.prior_clarification_options:
                    ctx.needs_clarification = build_clarification_from_options(
                        options=ctx.prior_clarification_options,
                        terms=ctx.prior_clarification_terms,
                    )
                    ctx.clarification_source = "phase2"
                    items = None  # signal: payload already built
                else:
                    items = [{"term": ctx.question[:60], "matches": [
                        {"table": tid.split(".")[-1],
                         "database": tid.split(".")[0] if "." in tid else "",
                         "column": ""} for tid in ctx.candidates[:5]]}]
            if items is not None:
                ctx.needs_clarification = build_clarification(items=items)
                ctx.clarification_source = "phase2"
        # Surface the resolved term→table bindings + any ambiguities so the UI
        # can show WHAT was disambiguated, not just the CLEAR/AMBIGUOUS status.
        mappings = [
            {"term": t, "table": v.get("table", ""),
             "database": v.get("database", ""), "confidence": v.get("confidence")}
            for t, v in (analysis.get("mappings") or {}).items()
            if isinstance(v, dict)
        ]
        _emit_phase(ctx, phase=2, action="phase_result",
                    status=analysis.get("status"),
                    mappings=mappings,
                    ambiguities=analysis.get("ambiguities") or [])
    return phase2


def _make_phase3(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 3 — slice builder + judge loop.

    Builds the slice from the Phase 1 candidates and expands it (via the judge's
    ``missing`` list) until sufficient or the round ceiling. The Phase 5
    grounding back-edge targets Phase 4 (SQL regeneration), not this node — a
    grounding failure means the model invented a column, which widening the
    slice cannot fix.
    """
    def phase3(_c: WorkflowContext) -> None:
        # The visit round is what keys this node's trace row in the frontend
        # (phase:step:round). phase_start and phase_result MUST use the SAME
        # round, or the start row is orphaned at "..." and the result lands on a
        # new row. ``visit_round`` is the entry visit (1 on first build, 2+ on a
        # grounding back-edge revisit); the judge's internal expand iterations
        # are reported separately as ``judgeRounds``.
        visit_round = ctx.phase3_rounds + 1
        _emit_phase(ctx, phase=3, action="phase_start", round=visit_round)
        ctx.slice_text = deps.builder.build(
            candidates=ctx.candidates, namespace=ctx.namespace,
        )
        # Per-round judge diagnostics (mirrors the VKG Phase 3 node). The loop
        # already computes the judge's `missing` list and re-fits the slice each
        # round, but only the aggregate outcome was emitted — so a
        # `phase3_max_rounds` degrade gave no signal as to WHY (was `missing`
        # converging? was the slice pegged at SLICE_TOKEN_BUDGET so truncation
        # kept evicting it?). Capture one record per round and attach it to
        # phase_result. No new LLM calls, no control-flow change.
        round_trace: List[Dict[str, Any]] = []

        def _slice_tokens() -> int:
            counter = getattr(deps.builder, "tokens", None)
            if not callable(counter):
                return 0
            try:
                return int(counter(ctx.slice_text))
            except Exception:  # noqa: BLE001 — diagnostics only, never break Phase 3
                return 0

        # Judge loop — expand until sufficient or the round ceiling is hit.
        rounds = 1
        while True:
            ok, judge_missing = deps.builder.is_sufficient(
                slice_text=ctx.slice_text, question=ctx.question,
            )
            round_trace.append({
                "round": rounds,
                "sufficient": bool(ok),
                "missing": list(judge_missing or []),
                "sliceTokens": _slice_tokens(),
            })
            # Self-contradiction override (deterministic, no LLM): the slice is
            # authoritative. If the judge says insufficient but EVERY entry it
            # named in `missing` is in fact present in the slice — by table /
            # column NAME, ignoring any schema/database qualifier — the judge has
            # contradicted the slice it was handed. The dominant false negative
            # here is a fabricated layer prefix lifted from the question text
            # (judge demands `curated.party` while the slice carries
            # `normalized.party`), which the softened JUDGE_PROMPT discourages but
            # cannot fully prevent. Trust the slice and proceed; the grounding
            # gate (Phase 5) remains the backstop against truly hallucinated SQL.
            # Only fires when `missing` is NON-EMPTY and FULLY present — a
            # genuinely-absent table/column still degrades. Mirrors the VKG
            # agent's override in ontology_query_agent/tier2/workflow.py.
            if not ok and judge_missing:
                table_names, qualified_columns = _slice_presence_index(ctx.slice_text)
                presence = {
                    m: _missing_entry_present(m, table_names, qualified_columns)
                    for m in judge_missing
                }
                logger.info("phase3.judge_missing_presence round=%d %s "
                            "(True=present in slice → judge false-negative; "
                            "False=genuinely absent)", rounds, presence)
                if all(presence.values()):
                    logger.info("phase3.override: judge said insufficient but all "
                                "%d missing entr(y/ies) are present in the slice — "
                                "trusting slice, proceeding to Phase 4.",
                                len(judge_missing))
                    ok = True
                    round_trace[-1]["overrodeJudgeFalseNegative"] = True
            if ok:
                break
            if rounds >= MAX_PHASE3_ROUNDS:
                ctx.degraded = "phase3_max_rounds"
                # Surface what the judge kept asking for so the user message can
                # name the genuine gap (e.g. a payout-frequency column that does
                # not exist on this layer) instead of a generic "narrow your
                # question". The final round's `missing` is the unmet need.
                unmet = [m for m in (judge_missing or []) if m]
                if unmet:
                    shown = ", ".join(unmet[:5])
                    ctx.degraded_detail = (
                        "I found relevant tables but the data needed to answer "
                        "this question isn't available in this semantic layer. "
                        f"Missing: {shown}. This usually means the column or "
                        "lookup doesn't exist in the underlying schema, so the "
                        "question can't be answered reliably here."
                    )
                break
            # Expand the slice with the judge's missing tables. When expand() can
            # add nothing fetchable, it returns a slice byte-identical to the one
            # just judged — re-judging it would produce the IDENTICAL verdict, so
            # the remaining rounds are pure waste (a wrong-but-stable "Missing: X"
            # for a table that doesn't exist in this layer, e.g. payout /
            # participant). Bail to the degrade now rather than burning the rest
            # of MAX_PHASE3_ROUNDS on a foregone conclusion.
            prev_slice = ctx.slice_text
            ctx.slice_text = deps.builder.expand(
                slice_text=ctx.slice_text, missing=judge_missing or [],
            )
            if ctx.slice_text == prev_slice:
                # Fabrication guard: the judge named only UNFETCHABLE tables
                # (expand added nothing). Narrowly distinguish a JUDGE-INVENTED
                # convenience table — a compound 'entityA_entityB' / 'entity_role'
                # / 'entity_owner' join-table name that does NOT exist anywhere in
                # this dataset (e.g. 'holding_party', 'policy_owner', 'party_role')
                # — from a plausible real-but-unbuilt single-entity table the layer
                # genuinely lacks (e.g. 'payout', 'participant'). Only the former is
                # a false negative we override: it fires solely when EVERY missing
                # entry matches the fabricated-compound shape AND every Phase-2
                # mapped table is already present, so the relationship is
                # expressible from the tables in hand (e.g. a life_participant
                # self-join). A plausible single-noun miss still degrades. Phase 5
                # grounding remains the backstop against truly hallucinated SQL.
                def _looks_fabricated(entry: str) -> bool:
                    bare = str(entry).split(".")[-1].lower()
                    parts = bare.split("_")
                    KNOWN = {"party", "holding", "policy", "coverage", "rider",
                             "owner", "role", "participant", "insured", "person"}
                    # a 2+-token compound built only from known entity/role words
                    # that isn't a real fetchable table = an invented join name.
                    return len(parts) >= 2 and all(p in KNOWN for p in parts)
                table_names, _qc = _slice_presence_index(ctx.slice_text)
                mapped = ctx.disambiguation or {}
                mapped_tables = set()
                for v in mapped.values():
                    name = v.get("table") if isinstance(v, dict) else v
                    if name:
                        mapped_tables.add(str(name).split(".")[-1].lower())
                core_present = bool(mapped_tables) and mapped_tables <= table_names
                all_fabricated = bool(judge_missing) and all(
                    _looks_fabricated(m) for m in judge_missing)
                if core_present and all_fabricated:
                    logger.info("phase3.fabrication_guard: judge missing=%s are all "
                                "invented compound table names and every Phase-2 "
                                "mapped table %s is in the slice — overriding the "
                                "false negative, proceeding to Phase 4 (grounding "
                                "gate is backstop).",
                                list(judge_missing or []), sorted(mapped_tables))
                    round_trace[-1]["overrodeJudgeFabrication"] = True
                    break  # leave loop with ctx.degraded unset → Phase 4
                logger.info("phase3.expand_noop: slice unchanged after expand "
                            "(no fetchable table in missing=%s) — short-circuiting "
                            "to degrade instead of re-judging an identical slice.",
                            list(judge_missing or []))
                ctx.degraded = "phase3_max_rounds"
                unmet = [m for m in (judge_missing or []) if m]
                if unmet:
                    shown = ", ".join(unmet[:5])
                    ctx.degraded_detail = (
                        "I found relevant tables but the data needed to answer "
                        "this question isn't available in this semantic layer. "
                        f"Missing: {shown}. This usually means the column or "
                        "lookup doesn't exist in the underlying schema, so the "
                        "question can't be answered reliably here."
                    )
                break
            rounds += 1
        ctx.phase3_rounds += rounds
        delta = dict(getattr(deps.builder, "judge_usage", {}) or {})
        add_usage(ctx, delta)
        _emit_phase(ctx, phase=3, action="phase_result",
                    round=visit_round, judgeRounds=rounds,
                    sufficient=(ctx.degraded is None),
                    tableCount=len(ctx.candidates),
                    # Per-round judge verdicts: [{round, sufficient, missing,
                    # sliceTokens}] — diagnoses a phase3_max_rounds degrade
                    # (convergence vs. budget-pegged truncation vs. judge
                    # false-negative). Compare sliceTokens vs SLICE_TOKEN_BUDGET.
                    judgeRoundsDetail=round_trace,
                    # The assembled slice (JSON string, already token-capped by
                    # the builder) so the UI can view + download the data that
                    # grounded SQL generation (todo item 2). Flows through
                    # phase_sink to the SSE stream and into the persisted
                    # phaseTimeline, so a reloaded session shows it too.
                    slice=ctx.slice_text,
                    inputTokens=delta.get("inputTokens", 0),
                    outputTokens=delta.get("outputTokens", 0))
    return phase3


def _make_phase3b(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 3b — slice-level disambiguation guard (on the 3→4 edge)."""
    try:
        from agents.shared.disambiguation_common import build_clarification
    except ImportError:  # container path: agents/ is on PYTHONPATH
        from shared.disambiguation_common import build_clarification  # type: ignore

    from .slice_disambiguation import (
        detect_unsupported_relationship,
        find_slice_ambiguities,
        parse_slice_obj,
    )

    def phase3b(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=3, action="phase_start", step="3b")
        slice_obj = parse_slice_obj(ctx.slice_text)
        result = find_slice_ambiguities(question=ctx.question, slice_obj=slice_obj)
        # Record heuristically-resolved bindings for the Phase 4 generator.
        if result.get("resolved"):
            ctx.disambiguation = {**ctx.disambiguation, **result["resolved"]}
        if result.get("ambiguous"):
            ctx.needs_clarification = build_clarification(items=result["items"])
            ctx.clarification_source = "phase3b"
        # Unsupported-relationship fast-fail: when the question compares two distinct
        # policy party-roles (e.g. insured vs policyholder) but the slice can't
        # represent one of them, degrade now rather than letting Phase 4 invent a
        # role column the grounding gate will reject (a wasted generate + 2 grounding
        # rounds — see session e7253c91). Only when not already clarifying.
        unsupported = detect_unsupported_relationship(
            question=ctx.question, slice_obj=slice_obj)
        if unsupported and ctx.needs_clarification is None:
            ctx.degraded = "relationship_unsupported"
            ctx.degraded_detail = unsupported
        _emit_phase(ctx, phase=3, action="phase_result", step="3b",
                    ambiguous=bool(result.get("ambiguous")),
                    resolvedHeuristically=bool(result.get("resolved")),
                    unsupportedRelationship=bool(unsupported))
    return phase3b


def _make_phase4(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 4 — SQL generate + sqlglot validate (1 repair inside generator)."""
    from .sql_validator import SqlSyntaxError

    def phase4(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=4, action="phase_start")
        # Clear the grounding back-edge flag so the Phase 5 re-check after this
        # (re)generation reflects the new SQL, not the prior round's verdict.
        ctx.grounding_missing = []
        repaired = False
        try:
            ctx.sql = deps.generator.generate(
                slice_text=ctx.slice_text, question=ctx.question,
                grounding_feedback=ctx.grounding_feedback,
            )
        except SqlSyntaxError:
            ctx.degraded = "sql_repair_failed"
            repaired = True
        # Roll the generator's token usage into the running total and report
        # this phase's delta in the trace event.
        delta = dict(getattr(deps.generator, "last_usage", {}) or {})
        add_usage(ctx, delta)
        _emit_phase(ctx, phase=4, action="phase_result",
                    repaired=repaired, regenerated=bool(ctx.grounding_feedback),
                    sql=ctx.sql, degraded=ctx.degraded,
                    inputTokens=delta.get("inputTokens", 0),
                    outputTokens=delta.get("outputTokens", 0))
    return phase4


def _make_phase5(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 5 — grounding gate (loop back / degrade) then bounded execution."""
    from .grounding import build_grounding_feedback, check_grounding

    def phase5(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=5, action="phase_start")
        missing = check_grounding(
            sql=ctx.sql, slice_text=ctx.slice_text, dialect="athena",
        )
        if missing:
            if ctx.grounding_rounds < MAX_GROUNDING_ROUNDS:
                # Loop back to Phase 4 (SQL regeneration) with the hallucinated
                # identifiers fed back as a negative constraint. The slice was
                # already judged sufficient, so the fix is to regenerate SQL
                # that uses only real slice columns — NOT to widen the slice
                # (which can't add columns that genuinely don't exist). The
                # feedback names the REAL columns of each flagged table + the
                # other slice tables, so regeneration can correct itself in-loop
                # instead of re-guessing a sibling column.
                ctx.grounding_missing = missing
                ctx.grounding_feedback = build_grounding_feedback(
                    missing=missing, slice_text=ctx.slice_text,
                )
                ctx.grounding_rounds += 1
                _emit_phase(ctx, phase=5, action="phase_result",
                            grounded=False, groundingRound=ctx.grounding_rounds,
                            missing=missing)
                return
            # Ceiling hit — degrade rather than execute un-grounded SQL.
            ctx.degraded = "grounding_unresolved"
            _emit_phase(ctx, phase=5, action="phase_result",
                        grounded=False, groundingRound=ctx.grounding_rounds,
                        degraded=ctx.degraded, missing=missing)
            return
        # Grounded — run the bounded execution agent. Pass the slice so it lands
        # in the execute_sql_query span for the SqlGrounded SESSION judge (the
        # slice otherwise only reaches the UI phase_sink, never an OTEL span).
        ctx.execution_result = deps.run_execution(
            ctx.sql, ctx.database_name, ctx.catalog_id,
            slice_text=ctx.slice_text,
        ) or {}
        delta = dict(ctx.execution_result.get("usage", {}) or {})
        add_usage(ctx, delta)
        _emit_phase(ctx, phase=5, action="phase_result", grounded=True,
                    rowCount=len(ctx.execution_result.get("rows", [])),
                    overLimit=bool(ctx.execution_result.get("over_limit")),
                    inputTokens=delta.get("inputTokens", 0),
                    outputTokens=delta.get("outputTokens", 0),
                    columns=ctx.execution_result.get("columns", []),
                    rows=ctx.execution_result.get("rows", []))
    return phase5


# ---------------------------------------------------------------------------
# Graph assembly + entry point (RAG-specific edges)
# ---------------------------------------------------------------------------
def build_tier2_graph(*, ctx: WorkflowContext, deps: PhaseDeps) -> Any:
    """Build the Strands Graph wiring the 6 phase nodes + 2 terminal nodes.

    Conditional edges and node functions both close over the shared ``ctx`` so
    routing flags (``degraded`` / ``needs_clarification`` / ``grounding_missing``)
    set by a node steer the next hop. Terminal nodes (clarification / degraded)
    are no-op sinks — the response is built from ``ctx`` after the run.

    The Phase 5 back-edge is RAG-specific: it always targets Phase 4
    (regenerate SQL). VKG assembles its own (hybrid) edges from the same shared
    nodes — see ``ontology_query_agent/tier2/workflow.py``.

    Args:
        ctx: The shared workflow context.
        deps: Injected phase implementations.
    """
    def _noop(_c: WorkflowContext) -> None:
        return None

    gb = GraphBuilder()
    gb.add_node(_FnNode(name="phase1", fn=_make_phase1(ctx, deps), ctx=ctx), "phase1")
    gb.add_node(_FnNode(name="phase2", fn=_make_phase2(ctx, deps), ctx=ctx), "phase2")
    gb.add_node(_FnNode(name="phase3", fn=_make_phase3(ctx, deps), ctx=ctx), "phase3")
    gb.add_node(_FnNode(name="phase3b", fn=_make_phase3b(ctx, deps), ctx=ctx), "phase3b")
    gb.add_node(_FnNode(name="phase4", fn=_make_phase4(ctx, deps), ctx=ctx), "phase4")
    gb.add_node(_FnNode(name="phase5", fn=_make_phase5(ctx, deps), ctx=ctx), "phase5")
    gb.add_node(_FnNode(name="clarify", fn=_noop, ctx=ctx), "clarify")
    gb.add_node(_FnNode(name="degraded", fn=_noop, ctx=ctx), "degraded")

    gb.set_entry_point("phase1")

    # Phase 1 → Phase 2 (candidates) | degraded (empty)
    gb.add_edge("phase1", "phase2", condition=lambda s: ctx.degraded != "phase1_empty")
    gb.add_edge("phase1", "degraded", condition=lambda s: ctx.degraded == "phase1_empty")

    # Phase 2 → Phase 3 (clear) | clarify (ambiguous)
    gb.add_edge("phase2", "phase3", condition=lambda s: ctx.needs_clarification is None)
    gb.add_edge("phase2", "clarify", condition=lambda s: ctx.needs_clarification is not None)

    # Phase 3 → Phase 3b (slice sufficient) | degraded (judge never reached
    # sufficiency within MAX_PHASE3_ROUNDS). Short-circuit the insufficient case
    # rather than generating + executing SQL against a slice the judge already
    # rejected (that yields a misleading 0-row answer). Mirrors the
    # grounding_unresolved gate (don't run an un-grounded query).
    gb.add_edge("phase3", "phase3b", condition=lambda s: ctx.degraded != "phase3_max_rounds")
    gb.add_edge("phase3", "degraded", condition=lambda s: ctx.degraded == "phase3_max_rounds")

    # Phase 3b → Phase 4 (clear) | clarify (ambiguous) | degraded (unsupported
    # relationship — the question compares two policy party-roles the model can't
    # represent; fast-fail rather than let Phase 4 invent a role column).
    gb.add_edge("phase3b", "phase4",
                condition=lambda s: ctx.needs_clarification is None
                and ctx.degraded != "relationship_unsupported")
    gb.add_edge("phase3b", "clarify", condition=lambda s: ctx.needs_clarification is not None)
    gb.add_edge("phase3b", "degraded",
                condition=lambda s: ctx.degraded == "relationship_unsupported")

    # Phase 4 → Phase 5 (ok) | degraded (sql_repair_failed)
    gb.add_edge("phase4", "phase5", condition=lambda s: ctx.degraded != "sql_repair_failed")
    gb.add_edge("phase4", "degraded", condition=lambda s: ctx.degraded == "sql_repair_failed")

    # Phase 5 → Phase 4 (ungrounded back-edge: regenerate SQL with feedback)
    #         | degraded (unresolved) | terminal (grounded)
    gb.add_edge("phase5", "phase4", condition=lambda s: bool(ctx.grounding_missing))
    gb.add_edge("phase5", "degraded", condition=lambda s: ctx.degraded == "grounding_unresolved")
    # grounded terminal: no out-edge needed — phase5 with nothing pending ends.

    gb.reset_on_revisit(True)
    gb.set_max_node_executions(MAX_NODE_EXECUTIONS)
    return gb.build()


def tier2_rag_workflow(*, question: str, namespace: str, kb_id: str,
                       deps: PhaseDeps,
                       phase_sink: Optional[Callable[[Optional[int], str, Dict[str, Any]], None]] = None,
                       clarification_resolution: Optional[Any] = None,
                       prior_clarification_options: Optional[List[Dict[str, Any]]] = None,
                       prior_clarification_terms: Optional[List[str]] = None,
                       ) -> WorkflowContext:
    """Run the Tier 2 RAG resolution graph and return the populated context.

    Args:
        question: The natural-language user question.
        namespace: Semantic-layer namespace for KB scoping.
        kb_id: Bedrock Knowledge Base id.
        deps: Injected phase implementations (router/builder/generator/run_execution).
        phase_sink: Optional live per-phase trace sink (streaming path).
        clarification_resolution: A
            :class:`agents.shared.clarification.ClarificationResolution` when
            this turn answers a prior clarification; Phase 1 prunes the rival
            candidates it names. ``None`` on a normal turn.
        prior_clarification_options: The ``[{id, label}]`` options the user was
            shown on the prior turn's clarification, passed in ONLY when this
            turn re-asks the SAME question (caller gates on original_question
            equality). Phase 2 reuses them for a no-specific-term low-confidence
            re-ask so the option set stays stable across turns. ``None`` on a
            first clarification or a new question.
        prior_clarification_terms: The ambiguous term(s) the prior clarification
            was about, reused alongside ``prior_clarification_options``.

    Returns:
        The :class:`WorkflowContext` after the graph completes — carries the
        slice, SQL, execution result, clarification payload, and degraded flag.
    """
    ctx = WorkflowContext(
        question=question, namespace=namespace, kb_id=kb_id,
        phase_sink=phase_sink,
        clarification_resolution=clarification_resolution,
        prior_clarification_options=list(prior_clarification_options or []),
        prior_clarification_terms=list(prior_clarification_terms or []),
    )
    return run_tier2_graph(
        ctx=ctx, build_graph=lambda c: build_tier2_graph(ctx=c, deps=deps),
    )
