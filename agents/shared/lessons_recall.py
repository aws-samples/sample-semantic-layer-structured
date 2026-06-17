"""Read side of AgentCore Memory — recall a user's past lessons to bias
disambiguation in a *later* session.

This is the counterpart to ``memory_hooks.persist_turn_pair`` (the writer). When
a query agent's term→table / term→IRI resolution is uncertain (AMBIGUOUS /
UNKNOWN / LOW_CONFIDENCE), it consults the user's long-term lessons before
asking the user to clarify. A prior fact like "the user's 'admin codes' means
the adminCode table" then resolves the term silently, so the user is not asked
the same question across sessions.

How it works:
  * ``recall_term_mappings`` runs a semantic search over the user's lessons
    namespace (``/lessons/<layerId>/<layerVersion>/<userId>/`` — a *prefix* that
    spans every prior session) and returns the matching lesson texts.
  * ``match_candidate`` scores each retrieved lesson against a (term, candidate)
    pair: a lesson that mentions BOTH the ambiguous term and the candidate's
    local name is evidence the user previously tied them together.

Both are fail-soft: a missing ``memory_id``, an SDK error, or an empty store
yields "no recollection" (``{}`` / ``None``) so the agent falls through to its
normal disambiguation flow. Recall is a *bias*, never a hard override.

Design choice — why prefix on layer+version+user (not session): cross-session
recall is the whole point, so we deliberately drop the ``{sessionId}`` segment.
We keep ``{semanticLayerVersion}`` so a re-modelled layer doesn't inherit a
mapping that may no longer hold (a renamed/dropped table).
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


def _local_name(identifier: str) -> str:
    """Return the lower-cased last segment of a table id / IRI / candidate id.

    Mirrors ``clarification.local_name`` so recalled lessons compare against
    candidates on the same footing (``normalized.admin_code`` → ``admin_code``,
    ``http://ex.org/onto#AdminCode`` → ``admincode``).

    Args:
        identifier: A candidate id, IRI, or table id.
    """
    s = (identifier or "").strip()
    for sep in ("#", "/", "."):
        if sep in s:
            s = s.rsplit(sep, 1)[1]
    return s.lower()


def _tokens(text: str) -> set:
    """Lower-cased alphanumeric word tokens of ``text`` (for whole-word checks)."""
    return set(re.findall(r"\b\w+\b", (text or "").lower()))


def recall_lessons(
    *,
    memory_id: str,
    semantic_layer_id: str,
    semantic_layer_version: str,
    user_id: str,
    query: str,
    top_k: int = 5,
    region: Optional[str] = None,
    manager_factory=None,
) -> List[str]:
    """Return the user's most relevant long-term lesson texts for ``query``.

    Searches the cross-session namespace prefix
    ``/lessons/<layerId>/<layerVersion>/<userId>/`` (NB: no ``{sessionId}`` — we
    want lessons from *prior* sessions). Fail-soft: returns ``[]`` on a missing
    ``memory_id``, an unconfigured user/layer, or any SDK error.

    Args:
        memory_id: AgentCore Memory id (the runtime's ``LESSONS_MEMORY_ID``).
        semantic_layer_id: Layer id (first namespace segment).
        semantic_layer_version: Active layer version (second segment).
        user_id: Cognito subject (third segment).
        query: The natural-language text to semantically search for (normally the
            user's question, or a specific ambiguous term).
        top_k: Number of top-scoring records to return.
        region: AWS region for the data-plane client; SDK default when ``None``.
        manager_factory: Test seam — zero-arg callable returning a
            ``MemorySessionManager``-shaped object. Production leaves this
            ``None`` and the real SDK manager is built.

    Returns:
        A list of lesson text strings (possibly empty), best match first.
    """
    if not memory_id or not semantic_layer_id or not semantic_layer_version \
            or not user_id:
        return []

    # Prefix WITHOUT a trailing session — search.namespace matches by hierarchy,
    # so this spans every prior session for this user+layer+version.
    namespace_prefix = (
        f"/lessons/{semantic_layer_id}/{semantic_layer_version}/{user_id}/"
    )
    try:
        if manager_factory is not None:
            manager = manager_factory()
        else:
            from bedrock_agentcore.memory.session import (  # type: ignore
                MemorySessionManager,
            )

            manager = MemorySessionManager(
                memory_id=memory_id, region_name=region
            )

        records = manager.search_long_term_memories(
            query,
            namespace_prefix,
            top_k,
        )
    except Exception as exc:  # noqa: BLE001 — recall is best-effort, never fatal
        logger.warning(
            "lessons recall failed (non-fatal) namespace=%s: %s",
            namespace_prefix, exc,
        )
        return []

    texts: List[str] = []
    for rec in records or []:
        # MemoryRecord is a dict-wrapper; the fact text lives at content.text.
        try:
            content = rec.get("content") if hasattr(rec, "get") else None
            text = (content or {}).get("text", "") if isinstance(content, dict) else ""
        except Exception:  # noqa: BLE001 — skip a malformed record
            text = ""
        if text:
            texts.append(text)
    return texts


def build_recall_resolver(
    *,
    memory_id: str,
    semantic_layer_id: str,
    semantic_layer_version: str,
    user_id: str,
    guardrail=None,
    region: Optional[str] = None,
    manager_factory=None,
):
    """Build the ``(term, candidate_ids) -> Optional[str]`` resolver for Phase 2.

    Returns ``None`` (recall disabled) when the memory resource or the
    layer/version/user scope is not fully known — the caller then passes ``None``
    as ``PhaseDeps.recall_resolver`` and Phase 2 behaves exactly as before.

    Otherwise returns a closure that, given an ambiguous ``term`` and the list of
    rival ``candidate_ids`` (table ids or IRIs), searches the user's lessons once
    and returns the single candidate a prior lesson ties the term to — or
    ``None`` when memory is silent or ambiguous (zero or >1 candidates match).

    The closure caches its per-term recall within a single resolution so a term
    appearing in multiple ambiguities only triggers one search.

    Args:
        memory_id: AgentCore Memory id (``LESSONS_MEMORY_ID``); empty disables.
        semantic_layer_id: Layer id (first namespace segment).
        semantic_layer_version: Active layer version (second segment).
        user_id: Cognito subject (third segment).
        guardrail: Unused today (recall reads already-redacted records); accepted
            for signature symmetry with the writer so callers wire them alike.
        region: AWS region for the data-plane client.
        manager_factory: Test seam forwarded to :func:`recall_lessons`.
    """
    if not memory_id or not semantic_layer_id or not semantic_layer_version \
            or not user_id:
        return None

    _cache: dict = {}

    def _resolve(term: str, candidate_ids: List[str]) -> Optional[str]:
        if term in _cache:
            lessons = _cache[term]
        else:
            lessons = recall_lessons(
                memory_id=memory_id,
                semantic_layer_id=semantic_layer_id,
                semantic_layer_version=semantic_layer_version,
                user_id=user_id,
                query=term,
                region=region,
                manager_factory=manager_factory,
            )
            _cache[term] = lessons
        if not lessons:
            return None
        # Keep candidates a recalled lesson actually supports. A unique survivor
        # is a confident resolution; zero or a tie means memory can't decide, so
        # we defer to the normal clarification flow.
        supported = [
            cid for cid in candidate_ids
            if match_candidate(term=term, candidate_id=cid, lessons=lessons)
        ]
        if len(supported) == 1:
            logger.info("lessons recall resolved term=%r -> %s", term, supported[0])
            return supported[0]
        return None

    return _resolve


def match_candidate(*, term: str, candidate_id: str, lessons: List[str]) -> bool:
    """True iff some recalled lesson ties ``term`` to ``candidate_id``.

    A lesson supports the (term, candidate) binding when its text contains BOTH
    the ambiguous term AND the candidate's local name as whole words — e.g. the
    lesson *"admin codes refers to the adminCode table"* supports binding the
    term ``codes`` (or the phrase ``admin codes``) to candidate ``adminCode``.

    This is a deliberately conservative, explainable check (whole-word
    co-occurrence) rather than another LLM call — recall sits in the hot path and
    must be cheap, and a false positive would silently misroute a query.

    Args:
        term: The ambiguous query term (or phrase) being resolved.
        candidate_id: A candidate table id / IRI under consideration.
        lessons: Recalled lesson texts from :func:`recall_lessons`.

    Returns:
        ``True`` when at least one lesson co-mentions the term and the
        candidate's local name.
    """
    cand_local = _local_name(candidate_id)
    if not cand_local or not term:
        return False
    term_words = set(re.findall(r"\b\w+\b", term.lower()))
    if not term_words:
        return False
    for lesson in lessons:
        words = _tokens(lesson)
        # The candidate local name may itself be multi-token after splitting on
        # underscores (admin_code → {admin, code}); require the whole local name
        # OR all of its underscore tokens to be present.
        cand_tokens = set(cand_local.split("_"))
        cand_hit = cand_local in words or (
            cand_tokens and cand_tokens.issubset(words)
        )
        # Every term word must appear (so "admin codes" needs both admin & codes).
        term_hit = term_words.issubset(words)
        if cand_hit and term_hit:
            return True
    return False
