"""Clarification-resolution — bind a user's reply to a prior clarification.

Both query agents (RAG metadata + VKG ontology) can pause a turn to ask a
disambiguation question ("Which interpretation of 'party' do you mean?") and
offer a list of options. Before this module, the *answer* to that question was
treated as a brand-new question: it was re-tokenized and fed back through the
same lexical disambiguator, which deterministically re-fired the identical
clarification — an infinite loop the user could never escape.

This module closes that gap with three pieces, all mode-agnostic so a single
implementation serves both agents:

  * :func:`build_pending_clarification` — packs the standalone question that
    triggered a clarification plus its offered options into a compact record the
    chat layer persists alongside the assistant turn (inside ``totals``).
  * :func:`load_pending_clarification` — reads that record off the most recent
    assistant turn of a session's history.
  * :func:`resolve_clarification_reply` — matches the user's next message
    against the offered options and, on an unambiguous single match, returns a
    :class:`ClarificationResolution` carrying the ORIGINAL question to re-run
    and the rival option ids to prune from Phase 1 candidates.

Design choices:
  * Option ids are bare names — a table name (``party_banking``) for RAG, a
    class local name (``EmailMessage``) for VKG. The matcher normalises both
    option ids and candidate ids to their last ``.`` / ``/`` / ``#`` segment so
    one rule covers both modes (see :func:`local_name`).
  * Fail-soft: any malformed record / ambiguous reply yields ``None`` so the
    turn falls through to the normal flow. A bad resolution must never break a
    turn or force-pick an interpretation the user did not choose.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Key under which the pending-clarification record is stored inside an assistant
# turn's ``totals`` map (persisted to the chat-sessions DDB row).
CLARIFICATION_TOTALS_KEY = "clarification"

# Hard ceiling (bytes) on the serialized pending-clarification record. A question
# with several independent ambiguities produces a CHAIN of clarifications, and the
# record accumulates one ``resolved`` entry per ambiguity already settled. Left
# unbounded the record can grow until the whole chat-sessions DDB item exceeds
# DynamoDB's 400 KB item-size limit (observed as an ``Item size … exceeded``
# UpdateItem error). 8 KB is far below that limit yet fits hundreds of compact
# resolutions; :func:`build_pending_clarification` trims the OLDEST ``resolved``
# entries (FIFO) to stay under it.
CLARIFICATION_RECORD_MAX_BYTES = 8192


@dataclass
class ResolvedChoice:
    """One already-resolved ambiguity in a multi-clarification chain.

    Persisted (without labels — see :data:`CLARIFICATION_RECORD_MAX_BYTES`) inside
    a pending record's ``resolved`` list and re-applied on every subsequent rerun,
    so a later clarification cannot "forget" an earlier choice.

    Attributes:
        chosen_id: The option id the user selected for this ambiguity.
        rival_ids: The option ids offered but NOT chosen — pruned from Phase 1.
        terms: The ambiguous term(s) this choice settled (for lesson persistence).
    """

    chosen_id: str
    rival_ids: List[str] = field(default_factory=list)
    terms: List[str] = field(default_factory=list)

    def to_record(self) -> Dict[str, Any]:
        """Serialize to the compact dict persisted under ``resolved`` (no labels)."""
        return {"chosen_id": self.chosen_id, "rival_ids": list(self.rival_ids),
                "terms": list(self.terms)}

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "ResolvedChoice":
        """Rebuild from a persisted ``resolved`` entry; tolerant of missing keys."""
        if not isinstance(record, dict):
            return cls(chosen_id="")
        return cls(
            chosen_id=record.get("chosen_id", "") or "",
            rival_ids=list(record.get("rival_ids", []) or []),
            terms=list(record.get("terms", []) or []),
        )


@dataclass
class ClarificationResolution:
    """A resolved clarification reply.

    Attributes:
        original_question: The standalone question that triggered the
            clarification — re-run verbatim instead of the bare reply, so the
            full query (filters, ordering, projection) is preserved.
        chosen_ids: Option ids the user's reply selected (normally one).
        rival_ids: Option ids that were offered but NOT chosen — pruned from the
            Phase 1 candidate set so the now-unambiguous term resolves cleanly.
    """

    original_question: str
    chosen_ids: List[str] = field(default_factory=list)
    rival_ids: List[str] = field(default_factory=list)
    # The ambiguous term(s) the clarification was about (e.g. ["codes"] or
    # ["admin codes"]). Carried so the agent can persist a crisp
    # "<term> → <chosen target>" lesson once resolved.
    terms: List[str] = field(default_factory=list)
    # Ambiguities resolved on EARLIER turns of this clarification chain. A
    # multi-ambiguity question (e.g. a question-level table-family choice AND a
    # term-level table choice) asks one clarification at a time; without carrying
    # the earlier choices forward, resolving the 2nd would re-open the 1st (Phase 1
    # rebuilds the full candidate set each rerun) and the chain never converges.
    # ``chosen_names`` / ``rival_names`` fold these in so the Phase 1 prune drops
    # every accumulated rival at once.
    prior: List[ResolvedChoice] = field(default_factory=list)

    @property
    def chosen_names(self) -> List[str]:
        """Lower-cased local names of all chosen option ids (this turn + prior)."""
        ids = list(self.chosen_ids) + [p.chosen_id for p in self.prior]
        return [local_name(i) for i in ids]

    @property
    def rival_names(self) -> List[str]:
        """Lower-cased local names of all rival ids (this turn + prior)."""
        ids = list(self.rival_ids)
        for p in self.prior:
            ids.extend(p.rival_ids)
        return [local_name(i) for i in ids]


def local_name(identifier: str) -> str:
    """Return the lower-cased last segment of a table id / IRI / option id.

    Normalises ``normalized.party_banking`` → ``party_banking`` and
    ``http://example.org/onto#EmailMessage`` → ``emailmessage`` so candidate
    ids (mode-specific shapes) and option ids (bare names) compare on equal
    footing.

    Args:
        identifier: A candidate id, IRI, or clarification option id/label.
    """
    s = (identifier or "").strip()
    # Split on the rightmost of the three structural separators IRIs/table-ids
    # use. ``#`` (IRI fragment) and ``/`` (IRI path) and ``.`` (db.table).
    for sep in ("#", "/", "."):
        if sep in s:
            s = s.rsplit(sep, 1)[1]
    return s.lower()


def build_pending_clarification(
    *, original_question: str, payload: Dict[str, Any],
    prior: Optional[List[ResolvedChoice]] = None,
) -> Dict[str, Any]:
    """Pack a clarification payload into a persistable pending record.

    Args:
        original_question: The standalone question that triggered the
            clarification (post-contextualization), so the next turn can re-run
            the real query rather than the bare option reply.
        payload: The ``needs_clarification`` dict the agent is about to return —
            carries ``options: [{id, label}, ...]``.
        prior: Ambiguities ALREADY resolved on earlier turns of this clarification
            chain (the current turn's resolution + everything before it). Stored
            under ``resolved`` so the next rerun re-applies them and the chain
            converges. ``None`` on the first clarification of a chain.

    Returns:
        A compact record ``{original_question, options, terms, resolved}`` suitable
        for storing in the assistant turn's ``totals[CLARIFICATION_TOTALS_KEY]``.
        Trimmed to stay under :data:`CLARIFICATION_RECORD_MAX_BYTES`.
    """
    options = payload.get("options") or []
    clean_options: List[Dict[str, str]] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        opt_id = opt.get("id") or ""
        if not opt_id:
            continue
        clean_options.append({"id": opt_id, "label": opt.get("label") or opt_id})
    resolved = [p.to_record() for p in (prior or [])]
    record = {
        "original_question": original_question,
        "options": clean_options,
        # Preserve the ambiguous term(s) so a resolved reply can be turned into a
        # crisp "<term> → <chosen>" lesson for AgentCore Memory.
        "terms": payload.get("terms") or [],
        # Accumulated earlier resolutions — re-applied by the Phase 1 prune on the
        # next rerun so a multi-ambiguity question converges (see ResolvedChoice).
        "resolved": resolved,
    }
    # Size guard: a long chain must not grow the DDB item past its limit. Drop the
    # OLDEST resolved entries (FIFO) until the record fits. A trimmed entry was
    # already applied to the candidate set on the turn it was made — the only cost
    # is that a re-asked identical clarification wouldn't auto-prune (rare).
    while (len(json.dumps(record).encode("utf-8")) > CLARIFICATION_RECORD_MAX_BYTES
           and record["resolved"]):
        record["resolved"].pop(0)
        logger.info("clarification record trimmed (size guard): %d resolved kept",
                    len(record["resolved"]))
    return record


def load_pending_clarification(
    history: Optional[List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Return the pending-clarification record off the latest assistant turn.

    Walks the history newest-first and returns the first assistant turn that
    carries a ``totals[CLARIFICATION_TOTALS_KEY]`` record. Returns ``None`` when
    the most recent assistant turn is not a clarification (a normal answered
    turn ends the clarification flow), or when history is empty.

    Args:
        history: Sliding-window history rows from
            ``ChatSessionService.history_window`` — ``[{role, text, totals?},
            ...]`` in chronological order.
    """
    if not history:
        return None
    for row in reversed(history):
        if not isinstance(row, dict):
            continue
        if row.get("role") != "assistant":
            continue
        # Only the MOST RECENT assistant turn matters: if it answered normally
        # (no clarification record), the clarification flow is over.
        totals = row.get("totals")
        if isinstance(totals, dict):
            record = totals.get(CLARIFICATION_TOTALS_KEY)
            if isinstance(record, dict) and record.get("options"):
                return record
        return None  # latest assistant turn was a normal answer — stop
    return None


def _reply_tokens(reply: str) -> List[str]:
    """Lower-cased word tokens of the user's reply (for whole-word matching)."""
    return re.findall(r"\b\w+\b", (reply or "").lower())


def resolve_clarification_reply(
    *, reply: str, pending: Optional[Dict[str, Any]],
) -> Optional[ClarificationResolution]:
    """Match a user's reply to exactly one offered clarification option.

    Matching tiers (first that yields a UNIQUE option wins):
      1. The reply, trimmed + lower-cased, equals an option ``id`` or ``label``.
      2. An option's local name (``local_name(id)``) appears as a whole word in
         the reply (covers "I mean party_banking" / "the banking one").

    Args:
        reply: The user's latest message (the answer to the clarification).
        pending: The record from :func:`load_pending_clarification`, or ``None``.

    Returns:
        A :class:`ClarificationResolution` on a single unambiguous match;
        ``None`` when there is no pending clarification, the reply matches zero
        or more-than-one options, or the record is malformed (fail-soft).
    """
    if not pending or not isinstance(pending, dict):
        return None
    options = pending.get("options") or []
    if not options:
        return None
    original_question = pending.get("original_question") or ""

    reply_norm = (reply or "").strip().lower()
    reply_words = set(_reply_tokens(reply))

    def _all_ids() -> List[str]:
        return [o.get("id", "") for o in options if isinstance(o, dict) and o.get("id")]

    # --- Tier 1: exact id / label match -----------------------------------
    exact: List[str] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        opt_id = opt.get("id") or ""
        if not opt_id:
            continue
        label = (opt.get("label") or "").strip().lower()
        if reply_norm and (reply_norm == opt_id.strip().lower() or reply_norm == label):
            exact.append(opt_id)
    matched = list(dict.fromkeys(exact))

    # --- Tier 2: whole-word local-name match ------------------------------
    if not matched:
        nameish: List[str] = []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            opt_id = opt.get("id") or ""
            if not opt_id:
                continue
            name = local_name(opt_id)
            if name and name in reply_words:
                nameish.append(opt_id)
        matched = list(dict.fromkeys(nameish))

    # A reply must select EXACTLY ONE option. Zero or many → no resolution, so
    # the turn falls through to the normal flow (at worst, clarify again).
    if len(matched) != 1:
        if matched:
            logger.info("clarification reply matched %d options (ambiguous) — "
                        "no resolution applied", len(matched))
        return None

    chosen = matched[0]
    rivals = [i for i in _all_ids() if i != chosen]
    terms = [t for t in (pending.get("terms") or []) if isinstance(t, str) and t]
    # Carry forward ambiguities settled on earlier turns of this chain so the
    # Phase 1 prune drops THEIR rivals too — otherwise resolving this clarification
    # re-opens the earlier ones and the chain never converges.
    prior = [ResolvedChoice.from_record(r)
             for r in (pending.get("resolved") or []) if isinstance(r, dict)]
    logger.info("clarification resolved: reply=%r -> %r (rivals pruned: %s; "
                "%d prior resolution(s) carried)", reply, chosen, rivals, len(prior))
    return ClarificationResolution(
        original_question=original_question,
        chosen_ids=[chosen],
        rival_ids=rivals,
        terms=terms,
        prior=prior,
    )


def accumulate_prior(
    resolution: Optional[Any],
) -> List[ResolvedChoice]:
    """Return the full accumulated resolution list to persist on the NEXT record.

    When a turn both *resolved* a clarification and *raised a new one*, the next
    pending record must carry every ambiguity settled so far: the resolutions
    inherited from earlier turns (``resolution.prior``) PLUS the one this turn just
    made (``resolution.chosen_ids`` / ``rival_ids`` / ``terms``). Passing the result
    as ``build_pending_clarification(..., prior=...)`` is what lets a
    multi-ambiguity question converge.

    Args:
        resolution: The :class:`ClarificationResolution` applied this turn, or
            ``None`` when this turn did not resolve a prior clarification (a first
            clarification in a chain) — then there is nothing to carry forward.

    Returns:
        The accumulated ``ResolvedChoice`` list (empty when ``resolution`` is
        ``None``).
    """
    if resolution is None:
        return []
    prior = list(getattr(resolution, "prior", []) or [])
    chosen_ids = list(getattr(resolution, "chosen_ids", []) or [])
    if chosen_ids:
        prior.append(ResolvedChoice(
            chosen_id=chosen_ids[0],
            rival_ids=list(getattr(resolution, "rival_ids", []) or []),
            terms=list(getattr(resolution, "terms", []) or []),
        ))
    return prior
