"""Tier 2 VKG resolution as a Strands multi-agent Graph workflow.

The VKG (SPARQL/Neptune) analog of the RAG workflow — a single graph:

    Phase 1  topic router          (KNN/lexical → candidate class+property IRIs)
    Phase 2  term disambiguation   (term → IRI; >1 class IRI → clarification)
    Phase 3  slice builder + judge  (SPARQL CONSTRUCT n_hops → Turtle slice)
    Phase 3b slice disambiguation   (property collision / multi class-path)
    Phase 4  SPARQL generate + validate (rdflib parseQuery + 1 repair)
    Phase 5  grounding gate + bounded execution agent
               - gate: triple-context IRI grounding against the slice;
                 a miss routes via the §0.1 HYBRID back-edge — a real-but-
                 out-of-slice IRI loops to Phase 3 (expand), a hallucinated /
                 misused IRI loops to Phase 4 (regenerate w/ feedback)
               - execute: run SPARQL on Neptune (gateway MCP), map to n_quads

The mode-agnostic primitives live in :mod:`agents.shared.tier2_graph`; this
module provides only the VKG phase-function factories and the VKG (hybrid) edge
assembly. The hybrid back-edge is the one place VKG genuinely differs from RAG
(which only ever loops Phase 5 → Phase 4): ``VkgSliceBuilder`` builds the slice
from a Neptune CONSTRUCT bounded by ``n_hops``, so a genuinely-existing
predicate can legitimately sit *outside* the slice and the fix is to widen it
(Phase 3 expand), not to regenerate.

All phases read and mutate a single shared :class:`WorkflowContext` that node
functions and conditional-edge predicates both close over.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

from strands.multiagent.graph import GraphBuilder

# Dual-import: repo root uses ``agents.shared``; the container has ``shared`` on
# PYTHONPATH directly (no top-level ``agents`` package).
try:
    from agents.shared.disambiguation_common import (
        _query_terms,
        build_clarification,
        inflection_variants,
    )
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
        run_tier2_graph,
    )
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.disambiguation_common import (  # type: ignore
        _query_terms,
        build_clarification,
        inflection_variants,
    )
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
        run_tier2_graph,
    )

try:
    from agents.shared.grounding_span import emit_grounding_span
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.grounding_span import emit_grounding_span  # type: ignore

logger = logging.getLogger(__name__)

# Universal human-readable-label attributes that EVERY entity carries. A
# question term equal to one of these is a descriptive column to RETURN, never an
# entity to disambiguate — so it must not drive a Phase-2 clarification. Kept
# deliberately narrow (NOT measures like amount/date/type, which can be a genuine
# cross-entity choice). Mirrors the Phase-3b ``_GENERIC_ATTRS`` deferral.
_GENERIC_LABEL_ATTRS = {
    "name", "names", "label", "labels", "description", "descriptions",
}

__all__ = [
    "PhaseDeps",
    "WorkflowContext",
    "SLICE_TOKEN_BUDGET",
    "build_vkg_graph",
    "tier2_vkg_workflow",
]


def _local_name(iri: str) -> str:
    """Return the lower-cased local name of ``iri`` (after the last ``/`` / ``#``)."""
    tail = iri.rstrip("/#")
    for sep in ("#", "/"):
        if sep in tail:
            tail = tail.rsplit(sep, 1)[1]
    if ":" in tail and "://" not in tail:
        tail = tail.rsplit(":", 1)[1]
    return tail.lower()


def _norm_token(name: str) -> str:
    """Normalize a local name for fuzzy comparison: lower-case, strip all
    non-alphanumerics, and drop trailing short coded-suffixes the judge tends to
    fabricate. So ``partyTypeTc`` / ``party_type_code`` / ``PartyTypeCode`` all
    collapse to ``partytype`` and compare equal — letting the false-negative
    override recognise a real property the judge merely mis-spelled, WITHOUT
    matching a genuinely different concept (``PolicyParticipant`` →
    ``policyparticipant`` matches nothing real).
    """
    t = "".join(ch for ch in (name or "").lower() if ch.isalnum())
    for suffix in ("code", "cd", "tc", "id", "sk", "key", "num"):
        if t.endswith(suffix) and len(t) > len(suffix) + 2:
            t = t[: -len(suffix)]
            break
    return t


def _iri_present_in_slice(iri: str, slice_text: str, slice_tokens: set) -> bool:
    """True when ``iri`` (a judge ``missing`` entry) is really in the slice.

    Two tiers: (1) exact local-name substring in the slice text (the strict check);
    (2) normalized fuzzy match of the local name against any slice local-name token
    (so a judge-fabricated mis-spelling like ``partyTypeTc`` matches the real
    ``party_type_code``). A genuinely-absent concept matches neither tier.
    """
    local = (iri.rsplit("/", 1)[-1] or iri)
    if local in (slice_text or ""):
        return True
    norm = _norm_token(local)
    return bool(norm) and norm in slice_tokens


# ---------------------------------------------------------------------------
# Phase node functions — closures over injected deps + shared ctx
# ---------------------------------------------------------------------------
def _make_phase1(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 1 — topic router: KNN/lexical → ranked candidate class/property IRIs."""
    def phase1(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=1, action="phase_start")
        ctx.candidates = deps.router.find_candidates(
            question=ctx.question, namespace=ctx.namespace,
        )
        # If this turn answers a prior clarification, drop the rival candidate
        # IRIs the user did not choose so Phase 2 sees the disambiguated term
        # owning a single IRI (no-op on a normal turn).
        apply_clarification_resolution(ctx)
        if not ctx.candidates:
            ctx.degraded = "phase1_empty"
        # Carry the ranked candidate detail so the UI can expand the Phase 1
        # chip into the actual class/property IRIs + relevance scores (not just
        # a bare count). ``kind`` is "class"/"property" — VKG candidates are
        # ontology IRIs, not tables.
        _emit_phase(ctx, phase=1, action="phase_result",
                    candidateCount=len(ctx.candidates),
                    candidates=list(getattr(deps.router, "last_candidates", []) or []),
                    candidateKind="iri",
                    degraded=ctx.degraded)
    return phase1


def _make_phase2(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 2 — term → IRI disambiguation over the Phase 1 candidate set.

    A question term whose local name maps to >1 distinct candidate IRI is
    ambiguous (which class/property did the user mean?) and surfaces a
    clarification. Resolved 1:1 mappings are recorded for the Phase 4 prompt.
    """
    def _candidates_for(token: str, by_name: Dict[str, List[str]]) -> List[str]:
        """Return candidate IRIs for a single token, in match-strength order.

        Resolution tiers (strongest first):
          1. EXACT local-name match (incl. simple plural/singular of the token);
          2. SUBSTRING match ("codes"/"admin" vs "admincodes", "policy" vs
             "policyproduct").

        A token that has any EXACT match returns ONLY the exact matches — a
        weaker substring match must never make an exact term look ambiguous
        (e.g. "email" exactly hitting ``EmailAddress`` must not be dragged into a
        clarification by also substring-hitting some ``…/email`` property). When
        there is no exact match we fall back to substring matches.
        """
        # All number-inflections of the token (policies↔policy, parties↔party)
        # — a naive rstrip('s') turned "policies" into "policie" and missed.
        token_forms = inflection_variants(token)

        # Tier 1: exact local-name match (token, or any of its inflected forms).
        exact: List[str] = []
        for key in token_forms:
            exact.extend(by_name.get(key, []))
        exact = list(dict.fromkeys(exact))
        if exact:
            return exact

        # Tier 2: substring fallback (only when no exact match exists).
        owners: List[str] = []
        long_forms = {f for f in token_forms if len(f) >= 4}
        if long_forms:
            for local, iris in by_name.items():
                if any(f in local or local in f for f in long_forms):
                    owners.extend(iris)
        return list(dict.fromkeys(owners))

    def _narrow_to_winner(matches: List[str], class_iris: set) -> Optional[str]:
        """Collapse multiple matched IRIs to a single winner, or ``None``.

        The topic router returns class IRIs and their property IRIs
        (``{classIri}/{prop}``). A term like "email" substring-matches a class
        (``EmailAddress``) AND several of its OWN properties (``emailType``,
        ``alternateEmail`` …) — that is NOT a genuine ambiguity; the user means
        the entity. So: if the matches contain exactly ONE class, resolve to it
        (the other matches are properties, typically of that same class).

        Returns the single class IRI, or ``None`` when the matches are a genuine
        tie — e.g. TWO distinct classes (``EmailMessage`` vs ``EmailCampaign``),
        or zero classes (only properties) — which still warrants a clarification.
        """
        class_matches = [m for m in matches if m in class_iris]
        if len(class_matches) == 1:
            return class_matches[0]
        return None

    def phase2(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=2, action="phase_start")
        terms = _query_terms(ctx.question)
        # Group candidate IRIs by lower-cased local name.
        by_name: Dict[str, List[str]] = {}
        for iri in ctx.candidates:
            by_name.setdefault(_local_name(iri), []).append(iri)
        # Set of CLASS IRIs among the candidates. A property IRI nests under its
        # class as ``{classIri}/{prop}``, so an IRI is a class when its parent
        # prefix is NOT itself a candidate (i.e. it is not a property of another
        # candidate). Used to collapse a "class + its own properties" match set.
        cand_set = set(ctx.candidates)
        class_iris = {
            iri for iri in ctx.candidates
            if iri.rsplit("/", 1)[0] not in cand_set
        }

        items: List[Dict[str, Any]] = []
        consumed: set = set()  # indices already resolved via a compound match

        # --- Pass 1: compound n-gram matching (bigrams then trigrams) -------
        # "admin codes" → concatenated "admincodes" → unambiguously hits
        # AdminCode without needing to resolve "admin" and "codes" separately.
        # A compound that resolves to exactly 1 IRI is marked CLEAR and its
        # constituent term indices are consumed so Pass 2 skips them.
        for n in (3, 2):  # try longer compounds first
            for i in range(len(terms) - n + 1):
                if any(j in consumed for j in range(i, i + n)):
                    continue
                compound = "".join(terms[i:i + n])  # e.g. "admin"+"codes" → "admincodes"
                unique = _candidates_for(compound, by_name)
                if len(unique) == 1:
                    label = " ".join(terms[i:i + n])
                    ctx.disambiguation[label] = {"status": "CLEAR", "iri": unique[0],
                                                 "confidence": 0.95}
                    consumed.update(range(i, i + n))

        # --- Pass 2: individual token matching for unconsumed terms ---------
        for idx, term in enumerate(terms):
            if idx in consumed:
                continue
            unique = _candidates_for(term, by_name)
            if not unique:
                continue
            if len(unique) == 1:
                ctx.disambiguation[term] = {"status": "CLEAR", "iri": unique[0],
                                            "confidence": 0.9}
                continue
            # Generic label-attribute deferral (mirrors the Phase-3b guard): a
            # universal descriptive attribute the user did not name as the head
            # entity — "name"/"label"/"description" — must NEVER drive a Phase-2
            # clarification, even if it happens to substring-match a CLASS local
            # name on a large layer (which would otherwise skip the property-only
            # guard below and clarify "Which interpretation of 'name'?"). The user
            # named the real entity elsewhere ("the policyholder's NAME"); bind
            # the term to its highest-ranked match and let the generator pick the
            # attribute on the resolved head entity. Kept narrow to label attrs
            # so a genuine entity term still clarifies.
            if term.lower() in _GENERIC_LABEL_ATTRS:
                rank = {iri: i for i, iri in enumerate(ctx.candidates)}
                best = min(unique, key=lambda iri: rank.get(iri, 1_000_000))
                ctx.disambiguation[term] = {"status": "CLEAR", "iri": best,
                                            "confidence": 0.6,
                                            "source": "generic_attr_top_rank"}
                continue
            # Multiple matches: a class + its own properties (or a clearly
            # top-ranked class) is not a real ambiguity — resolve to the winner.
            winner = _narrow_to_winner(unique, class_iris)
            if winner is not None:
                ctx.disambiguation[term] = {"status": "CLEAR", "iri": winner,
                                            "confidence": 0.8}
                continue
            # Genuinely ambiguous on this ontology — but THIS user may have
            # resolved the same term in a prior session. Consult long-term
            # lessons before surfacing a clarification.
            recalled = None
            if deps.recall_resolver is not None:
                try:
                    recalled = deps.recall_resolver(term, unique)
                except Exception:  # noqa: BLE001 — recall must never break Phase 2
                    recalled = None
            if recalled in unique:
                ctx.disambiguation[term] = {"status": "CLEAR", "iri": recalled,
                                            "confidence": 0.8, "source": "memory"}
                continue
            # PROPERTY-ONLY multi-match: the matched IRIs are ALL properties (no
            # class IRI among them) — e.g. "name" hits party.name, product.name,
            # … across many classes; "hold" hits only holding_* properties. A set
            # of sibling properties spread across classes is NOT a genuine ENTITY
            # ambiguity the user can resolve ("Which interpretation of 'name'?" is
            # un-actionable — every option is a *_name attribute, not a thing to
            # pick). The user already named the real entity elsewhere in the
            # question (e.g. "the policyholder's NAME", "coverage products by
            # NAME"); the descriptive attribute term must not hijack a
            # clarification. Defer to Phase-1 RANKING: bind the highest-ranked
            # matching IRI (earliest in ctx.candidates) at reduced confidence and
            # let Phase 3 widen / the generator pick the right column. This holds
            # WHETHER OR NOT the term exactly names a property — an exact hit on a
            # property literally called "name" is still not an entity choice. A
            # genuine tie between TWO+ distinct CLASSES (EmailMessage vs
            # EmailCampaign) is handled below (class_matches non-empty) and still
            # clarifies; a real property-DOMAIN collision is caught later by Phase
            # 3b against the assembled slice (see test_property_collision_clarifies).
            class_matches = [iri for iri in unique if iri in class_iris]
            if not class_matches:
                rank = {iri: i for i, iri in enumerate(ctx.candidates)}
                best = min(unique, key=lambda iri: rank.get(iri, 1_000_000))
                ctx.disambiguation[term] = {"status": "CLEAR", "iri": best,
                                            "confidence": 0.6,
                                            "source": "fuzzy_top_rank"}
                continue
            # Sibling-class collapse: a head noun ("hold", "holding") substring-
            # matches a BASE entity class AND its derived siblings (Holding,
            # HoldingLoan, HoldingSubaccount, HoldingPayout, …). That is not a
            # real entity ambiguity — the user means the base entity. When the
            # matched classes share a common BASE whose local name is itself one
            # of the matches (the shortest, and a prefix of the others), resolve
            # to that base class instead of clarifying. Larger ontologies (the
            # 40-table VKG layer) expose many such sibling classes; the flatter
            # metadata slice does not, which is why only the VKG path over-clarified.
            if len(class_matches) >= 2:
                names = {iri: _local_name(iri) for iri in class_matches}
                shortest = min(class_matches, key=lambda iri: len(names[iri]))
                base = names[shortest]
                if base and all(names[iri].startswith(base) for iri in class_matches):
                    ctx.disambiguation[term] = {
                        "status": "CLEAR", "iri": shortest, "confidence": 0.7,
                        "source": "base_class_collapse"}
                    continue
            # Carry the FULL IRI as the option id (``table``) so a later
            # clarification-reply resolution can seed the chosen CLASS back into
            # Phase 1 as a real IRI — the seed reconstructs ``db.table`` for RAG
            # but VKG candidates are IRIs (no ``db``), so a bare local name like
            # "party" seeded as-is is not a fetchable class and the slice builder
            # silently drops it (Bug: reply "party" resolved to Relation). The
            # human-readable ``label`` still shows the local name (built by
            # build_clarification from ``table`` — IRIs render ugly, so pass the
            # local name as the label via a dedicated key the builder honors).
            items.append({"term": term, "matches": [
                {"table": iri, "database": "",
                 "column": "", "label": _local_name(iri)}
                for iri in unique]})

        if items:
            ctx.needs_clarification = build_clarification(items=items)
            ctx.clarification_source = "phase2"
        # Surface the resolved term→IRI bindings + any ambiguities so the UI can
        # show WHAT was disambiguated, not just "clear".
        mappings = [
            {"term": t, "iri": v.get("iri", ""),
             "localName": _local_name(v.get("iri", "")),
             "confidence": v.get("confidence")}
            for t, v in ctx.disambiguation.items() if isinstance(v, dict)
        ]
        _emit_phase(ctx, phase=2, action="phase_result",
                    status=("AMBIGUOUS" if items else "CLEAR"),
                    mappings=mappings, ambiguities=items)
    return phase2


def _make_phase3(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 3 — slice builder + judge loop (CONSTRUCT n_hops → Turtle slice).

    On a Phase 5 *expand* back-edge (``grounding_route == "expand"``), the
    out-of-slice IRIs collected in ``grounding_missing`` are folded into the
    candidate set so the CONSTRUCT pulls their neighborhood in — the legitimate
    "slice too narrow" fix.
    """
    def phase3(_c: WorkflowContext) -> None:
        # Absorb an expand back-edge: widen the candidate set, then clear the
        # routing flags so a fresh grounding verdict drives the next hop.
        if ctx.grounding_route == "expand" and ctx.grounding_missing:
            ctx.candidates = list(dict.fromkeys(ctx.candidates + ctx.grounding_missing))
            ctx.grounding_missing = []
            ctx.grounding_route = None
        # phase_start and phase_result MUST share the same round, or the start
        # trace row is orphaned at "..." (frontend keys rows by phase:step:round).
        # ``visit_round`` is the entry visit; judge expand iterations are
        # reported separately as ``judgeRounds``.
        visit_round = ctx.phase3_rounds + 1
        _emit_phase(ctx, phase=3, action="phase_start", round=visit_round)
        ctx.slice_text = deps.builder.build(
            candidates=ctx.candidates, namespace=ctx.namespace,
        )
        # Per-round judge diagnostics. The loop already computes the judge's
        # `missing` list and re-fits the slice each round, but historically only
        # the aggregate outcome was emitted — so a `phase3_max_rounds` degrade
        # gave no signal as to WHY (was `missing` converging? was the slice
        # pegged at SLICE_TOKEN_BUDGET so truncation kept evicting it?). Capture
        # one record per round and attach it to phase_result so the reasoning
        # panel / batch-eval JSON shows whether the right knob is rounds, the
        # token budget, n_hops, or judge calibration. No new LLM calls, no
        # control-flow change — `tokens()` is the builder's existing counter.
        round_trace: List[Dict[str, Any]] = []

        def _slice_tokens() -> int:
            counter = getattr(deps.builder, "tokens", None)
            if not callable(counter):
                return 0
            try:
                return int(counter(ctx.slice_text))
            except Exception:  # noqa: BLE001 — diagnostics only, never break Phase 3
                return 0

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
            # Self-contradiction override (deterministic, no LLM): the slice text
            # is authoritative. If the judge says insufficient but EVERY IRI it
            # named in `missing` is in fact present in the slice (by local name),
            # the judge has contradicted the slice it was handed — a known false
            # negative on this path (verified via diagnostic logging: the judge
            # flagged CoverageProduct / CoverageProduct/productName as missing
            # while both were in the slice). Trust the slice over the judge and
            # proceed. Only overrides when the missing list is NON-EMPTY and
            # FULLY present — a genuinely-absent IRI still degrades. SqlGrounded
            # remains the downstream backstop against hallucinated schema.
            if not ok and judge_missing:
                slc = ctx.slice_text or ""
                # Normalized local-name tokens present in the slice — used for the
                # fuzzy tier so a judge-fabricated mis-spelling (partyTypeTc) still
                # matches the real property (party_type_code). Slice IRIs look like
                # .../Class or .../Class/prop; take each path tail.
                slice_tokens = {
                    _norm_token(tok.rsplit("/", 1)[-1])
                    for tok in re.findall(r"<[^>]+>|[A-Za-z_][\w/]*", slc)
                }
                slice_tokens.discard("")
                presence = {
                    m: _iri_present_in_slice(m, slc, slice_tokens)
                    for m in judge_missing
                }
                logger.info("phase3.judge_missing_presence round=%d %s "
                            "(True=present in slice → judge false-negative; "
                            "False=genuinely absent)", rounds, presence)
                if all(presence.values()):
                    logger.info("phase3.override: judge said insufficient but all "
                                "%d missing IRI(s) are present in the slice — "
                                "trusting slice, proceeding to Phase 4.",
                                len(judge_missing))
                    ok = True
                    round_trace[-1]["overrodeJudgeFalseNegative"] = True
            if ok:
                break
            if rounds >= MAX_PHASE3_ROUNDS:
                ctx.degraded = "phase3_max_rounds"
                # Surface what the judge kept asking for so the user message can
                # name the genuine gap (e.g. a concept/property the ontology does
                # not model) instead of a generic "narrow your question". The
                # final round's `missing` is the unmet need.
                unmet = [m for m in (judge_missing or []) if m]
                if unmet:
                    shown = ", ".join(unmet[:5])
                    ctx.degraded_detail = (
                        "I found relevant ontology concepts but the data needed "
                        "to answer this question isn't available in this semantic "
                        f"layer. Missing: {shown}. This usually means the concept "
                        "or property isn't modelled in the ontology, so the "
                        "question can't be answered reliably here."
                    )
                break
            # Expand the slice with the judge's missing IRIs. When expand() can
            # add nothing (no fetchable IRI in `missing`), it returns a slice
            # identical to the one just judged — re-judging would yield the
            # IDENTICAL verdict, so the remaining rounds are pure waste. Bail to
            # the degrade now rather than burning the rest of MAX_PHASE3_ROUNDS.
            prev_slice = ctx.slice_text
            ctx.slice_text = deps.builder.expand(
                slice_text=ctx.slice_text, missing=judge_missing or [],
            )
            if ctx.slice_text == prev_slice:
                logger.info("phase3.expand_noop: slice unchanged after expand "
                            "(no fetchable IRI in missing=%s) — short-circuiting "
                            "to degrade instead of re-judging an identical slice.",
                            list(judge_missing or []))
                ctx.degraded = "phase3_max_rounds"
                unmet = [m for m in (judge_missing or []) if m]
                if unmet:
                    shown = ", ".join(unmet[:5])
                    ctx.degraded_detail = (
                        "I found relevant ontology concepts but the data needed "
                        "to answer this question isn't available in this semantic "
                        f"layer. Missing: {shown}. This usually means the concept "
                        "or property isn't modelled in the ontology, so the "
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
                    classCount=len(ctx.candidates),
                    # Per-round judge verdicts: [{round, sufficient, missing,
                    # sliceTokens}] — diagnoses a phase3_max_rounds degrade
                    # (convergence vs. budget-pegged truncation vs. judge
                    # false-negative). Compare sliceTokens against
                    # SLICE_TOKEN_BUDGET (12000).
                    judgeRoundsDetail=round_trace,
                    # The assembled ontology slice (Turtle string, already
                    # budget-capped by the builder) so the UI can view +
                    # download the grounding data — the VKG analogue of the RAG
                    # slice view. Unlike RAG (JSON), this is Turtle/RDF; the
                    # frontend detects the format and renders/downloads as .ttl.
                    # Flows through phase_sink → SSE → persisted phaseTimeline.
                    slice=ctx.slice_text,
                    inputTokens=delta.get("inputTokens", 0),
                    outputTokens=delta.get("outputTokens", 0))
    return phase3


def _make_phase3b(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 3b — VKG slice-level disambiguation guard (on the 3→4 edge)."""
    from .slice_disambiguation import find_slice_ambiguities

    def phase3b(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=3, action="phase_start", step="3b")
        result = find_slice_ambiguities(
            question=ctx.question, slice_graph=ctx.slice_text,
        )
        if result.get("resolved"):
            ctx.disambiguation = {**ctx.disambiguation, **result["resolved"]}
        if result.get("ambiguous"):
            ctx.needs_clarification = build_clarification(items=result["items"])
            ctx.clarification_source = "phase3b"
        _emit_phase(ctx, phase=3, action="phase_result", step="3b",
                    ambiguous=bool(result.get("ambiguous")),
                    resolvedHeuristically=bool(result.get("resolved")))
    return phase3b


def _make_phase4(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 4 — SPARQL generate + rdflib validate (1 repair inside generator)."""
    from .sparql_validator import SparqlSyntaxError

    def phase4(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=4, action="phase_start")
        # Clear the grounding back-edge flags so the Phase 5 re-check after this
        # (re)generation reflects the new SPARQL, not the prior round's verdict.
        ctx.grounding_missing = []
        ctx.grounding_route = None
        repaired = False
        try:
            ctx.sparql_query = deps.generator.generate(
                slice_text=ctx.slice_text, question=ctx.question,
                grounding_feedback=ctx.grounding_feedback,
            )
        except SparqlSyntaxError:
            ctx.degraded = "sparql_repair_failed"
            repaired = True
        delta = dict(getattr(deps.generator, "last_usage", {}) or {})
        add_usage(ctx, delta)
        _emit_phase(ctx, phase=4, action="phase_result",
                    repaired=repaired, regenerated=bool(ctx.grounding_feedback),
                    sparql=ctx.sparql_query, degraded=ctx.degraded,
                    inputTokens=delta.get("inputTokens", 0),
                    outputTokens=delta.get("outputTokens", 0))
    return phase4


def _make_phase5(ctx: WorkflowContext, deps: PhaseDeps) -> Callable[[WorkflowContext], None]:
    """Phase 5 — grounding gate (hybrid back-edge / degrade) then execution."""
    from .grounding import check_grounding, classify_missing

    def phase5(_c: WorkflowContext) -> None:
        _emit_phase(ctx, phase=5, action="phase_start")
        missing = check_grounding(
            sparql=ctx.sparql_query, slice_graph_or_text=ctx.slice_text,
        )
        if missing:
            classified = classify_missing(
                missing, candidates=ctx.candidates,
                neptune_probe=getattr(deps, "neptune_probe", None),
            )
            expand = classified["expand"]
            regenerate = classified["regenerate"]
            if ctx.grounding_rounds < MAX_GROUNDING_ROUNDS:
                ctx.grounding_rounds += 1
                # Always feed hallucinated/misused IRIs back as a negative
                # constraint so whenever Phase 4 next runs (directly, or after a
                # Phase 3 expand) it avoids them.
                if regenerate:
                    ctx.grounding_feedback = ", ".join(regenerate)
                # Prefer widening the slice when a real IRI sits out-of-slice;
                # otherwise regenerate. (A pure-regenerate miss never spins the
                # slice builder.)
                if expand:
                    ctx.grounding_missing = expand
                    ctx.grounding_route = "expand"
                else:
                    ctx.grounding_route = "regenerate"
                _emit_phase(ctx, phase=5, action="phase_result",
                            grounded=False, groundingRound=ctx.grounding_rounds,
                            route=ctx.grounding_route, missing=missing,
                            sparql=ctx.sparql_query)
                return
            # Ceiling hit — degrade rather than execute un-grounded SPARQL.
            ctx.degraded = "grounding_unresolved"
            _emit_phase(ctx, phase=5, action="phase_result",
                        grounded=False, groundingRound=ctx.grounding_rounds,
                        degraded=ctx.degraded, missing=missing,
                        sparql=ctx.sparql_query)
            return
        # Grounded — run the bounded execution agent.
        ctx.execution_result = deps.run_execution(ctx.sparql_query) or {}
        # Propagate a Phase-5 execution failure (sparql_translation_failed /
        # sql_execution_failed, set on the execution-result dict by
        # main._run_execution) onto ctx.degraded so invoke()'s response builder
        # maps it to a clean user-facing answer (Task 11). This only runs on the
        # executed path: the grounding gate above sets ctx.degraded=
        # "grounding_unresolved" and RETURNS before reaching here, so it is
        # never clobbered.
        exec_degraded = ctx.execution_result.get("degraded")
        if exec_degraded:
            ctx.degraded = exec_degraded
        else:
            # Eval-only telemetry: VKG Phase 5 is deterministic (Ontop translate
            # → Athena, no LLM), so the SDK emits no harvested span and the
            # SESSION-level SqlGrounded judge would see the executed SQL but no
            # ontology slice to verify it against — failing closed. Emit a span
            # in the strands.telemetry.tracer scope carrying the slice (input) +
            # executed SQL (output) so the judge can ground. Only on the
            # successful (non-degraded) executed path, where both are present.
            # Fail-soft inside the helper — never breaks the query.
            emit_grounding_span(
                retrieved_schema=ctx.slice_text or "",
                executed_sql=ctx.execution_result.get("sql", "") or "",
                question=ctx.question,
            )
        delta = dict(ctx.execution_result.get("usage", {}) or {})
        add_usage(ctx, delta)
        _emit_phase(ctx, phase=5, action="phase_result", grounded=True,
                    rowCount=len(ctx.execution_result.get("rows", [])),
                    tripleCount=len(ctx.execution_result.get("n_quads", [])),
                    overLimit=bool(ctx.execution_result.get("over_limit")),
                    inputTokens=delta.get("inputTokens", 0),
                    outputTokens=delta.get("outputTokens", 0),
                    columns=ctx.execution_result.get("columns", []),
                    rows=ctx.execution_result.get("rows", []))
        # NOTE: on the grounded-success path the answer is now rendered by a real
        # bounded LLM inside deps.run_execution (main._render_answer), which emits
        # a genuine in-graph `chat` model span (real model id + usage, NL output)
        # the SESSION FinalAnswerFaithfulness judge captures. We therefore do NOT
        # emit the synthetic answer_emitter span here — it would be a redundant,
        # zero-usage span emitted AFTER the real one and could shadow it. The
        # answer_emitter is still used at the clarify / degraded terminals below,
        # which have no LLM answer call of their own.
    return phase5


# ---------------------------------------------------------------------------
# Graph assembly + entry point (VKG-specific hybrid edges)
# ---------------------------------------------------------------------------
def build_vkg_graph(*, ctx: WorkflowContext, deps: PhaseDeps) -> Any:
    """Build the Strands Graph wiring the VKG phase nodes + 2 terminal nodes.

    Differs from the RAG graph only in the Phase 5 back-edge, which is a hybrid
    (§0.1): ``grounding_route == "expand"`` loops to Phase 3, ``"regenerate"``
    loops to Phase 4. ``grounding_route`` is set to exactly one value per round
    so the two back-edges are mutually exclusive.

    Args:
        ctx: The shared workflow context.
        deps: Injected phase implementations.
    """
    def _emit_terminal_answer(_c: WorkflowContext) -> None:
        """Terminal-node body: emit the eval final-answer span IN-GRAPH.

        Runs as the ``clarify`` / ``degraded`` terminal node, i.e. while the
        graph's multiagent span is still the active (recording) OTEL context —
        the only position the SESSION harvester treats as the conversation's
        final answer (a post-graph emit orphans into a separate trace; see
        PhaseDeps.answer_emitter). Fail-soft: the emitter swallows its own
        errors, and a missing emitter is a no-op.
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

    # Phase 1 → Phase 2 (candidates) | degraded (empty)
    gb.add_edge("phase1", "phase2", condition=lambda s: ctx.degraded != "phase1_empty")
    gb.add_edge("phase1", "degraded", condition=lambda s: ctx.degraded == "phase1_empty")

    # Phase 2 → Phase 3 (clear) | clarify (ambiguous)
    gb.add_edge("phase2", "phase3", condition=lambda s: ctx.needs_clarification is None)
    gb.add_edge("phase2", "clarify", condition=lambda s: ctx.needs_clarification is not None)

    # Phase 3 → Phase 3b (slice sufficient) | degraded (judge never reached
    # sufficiency within MAX_PHASE3_ROUNDS). Short-circuit the insufficient case
    # rather than generating + executing SPARQL against a slice the judge already
    # rejected — that produced a misleading 0-row answer. Mirrors the
    # grounding_unresolved gate below (don't run an un-grounded query).
    gb.add_edge("phase3", "phase3b", condition=lambda s: ctx.degraded != "phase3_max_rounds")
    gb.add_edge("phase3", "degraded", condition=lambda s: ctx.degraded == "phase3_max_rounds")

    # Phase 3b → Phase 4 (clear) | clarify (ambiguous)
    gb.add_edge("phase3b", "phase4", condition=lambda s: ctx.needs_clarification is None)
    gb.add_edge("phase3b", "clarify", condition=lambda s: ctx.needs_clarification is not None)

    # Phase 4 → Phase 5 (ok) | degraded (sparql_repair_failed)
    gb.add_edge("phase4", "phase5", condition=lambda s: ctx.degraded != "sparql_repair_failed")
    gb.add_edge("phase4", "degraded", condition=lambda s: ctx.degraded == "sparql_repair_failed")

    # Phase 5 HYBRID back-edge:
    #   route == "expand"     → Phase 3 (widen the slice; real IRI out-of-slice)
    #   route == "regenerate" → Phase 4 (rewrite SPARQL; hallucinated/misused)
    #   degraded              → degraded terminal
    #   grounded              → terminal (no out-edge)
    gb.add_edge("phase5", "phase3", condition=lambda s: ctx.grounding_route == "expand")
    gb.add_edge("phase5", "phase4", condition=lambda s: ctx.grounding_route == "regenerate")
    gb.add_edge("phase5", "degraded", condition=lambda s: ctx.degraded == "grounding_unresolved")

    gb.reset_on_revisit(True)
    gb.set_max_node_executions(MAX_NODE_EXECUTIONS)
    return gb.build()


def tier2_vkg_workflow(*, question: str, namespace: str,
                       deps: PhaseDeps,
                       phase_sink: Optional[Callable[[Optional[int], str, Dict[str, Any]], None]] = None,
                       clarification_resolution: Optional[Any] = None,
                       ) -> WorkflowContext:
    """Run the Tier 2 VKG resolution graph and return the populated context.

    Args:
        question: The natural-language user question.
        namespace: Semantic-layer namespace for Neptune/KNN scoping.
        deps: Injected phase implementations (router/builder/generator/run_execution).
        phase_sink: Optional live per-phase trace sink (streaming path).
        clarification_resolution: A
            :class:`agents.shared.clarification.ClarificationResolution` when
            this turn answers a prior clarification; Phase 1 prunes the rival
            candidate IRIs it names. ``None`` on a normal turn.

    Returns:
        The :class:`WorkflowContext` after the graph completes — carries the
        slice, SPARQL, execution result, clarification payload, and degraded flag.
    """
    ctx = WorkflowContext(
        question=question, namespace=namespace, phase_sink=phase_sink,
        clarification_resolution=clarification_resolution,
    )
    return run_tier2_graph(
        ctx=ctx, build_graph=lambda c: build_vkg_graph(ctx=c, deps=deps),
    )
