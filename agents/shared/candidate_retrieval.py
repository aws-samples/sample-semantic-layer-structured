"""Shared candidate-retrieval helpers for query agents.

Query agents ground a natural-language question against an ontology by naming
candidate concepts. Those names arrive in two shapes: a full IRI (already
resolved) or a bare label the model guessed (e.g. ``"Customers"``). Downstream
graph construction only pulls a concept in when handed its real IRI, so a bare
label must be mapped back to the IRI it names first.

``resolve_to_iri`` performs that mapping. It was lifted verbatim (behavior-wise)
from ``ontology_query_agent.tier2.vkg_slice_builder.VkgSliceBuilder._resolve_to_iri``
so multiple agents can reuse the same resolution semantics.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List

# Imported as a module-level name so tests (and callers) can monkeypatch
# ``candidate_retrieval.embed_text`` on this module and have it take effect.
# Use a RELATIVE import (matching sibling shared modules metric_lookup /
# knn_hydration) so this resolves under BOTH the repo package ``agents.shared``
# and the container package ``shared`` (where ``agents`` is not importable — a
# hard ``from agents.shared.embedding`` raises ModuleNotFoundError at agent boot).
from .embedding import embed_text

_LOG = logging.getLogger(__name__)

# Process-wide cache keyed by the exact concept-text string. Embedding is the
# expensive (network) step, so caching by text bounds cost across repeated
# calls that reference the same ontology concepts.
_EMBED_CACHE: Dict[str, List[float]] = {}


def resolve_to_iri(token: str, ontology_json: Dict[str, Any]) -> str:
    """Resolve a concept ``token`` to a real ontology IRI.

    A model naming a missing concept is told to use an IRI present in the slice,
    but when the concept was never in the slice it can only name it by a guessed
    label (e.g. ``"Customers"``). A raw union of that label into the candidate
    set pulls nothing on re-construct. So map each token to a full IRI by a
    case-insensitive match against the FULL ontology's class/property local-names
    and ``label`` fields, over both the ``classes`` and ``properties`` sections.

    Args:
        token: A concept reference — either already an IRI or a bare label.
        ontology_json: The FULL ontology, shaped as
            ``{"classes": {iri: {"label": ...}}, "properties": {iri: {...}}}``.

    Returns:
        The resolved full IRI, or ``token`` unchanged when it is already an IRI
        or no ontology match is found (no worse than prior behavior).
    """
    if "://" in token:  # already an IRI
        return token
    needle = token.strip().lower()
    if not needle:
        return token
    # Scan both ontology sections; match the token case-insensitively against
    # each IRI's local-name (last segment after the final "/" or "#") or label.
    for section in ("classes", "properties"):
        for iri, meta in (ontology_json.get(section, {}) or {}).items():
            local = iri.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            label = str((meta or {}).get("label", ""))
            if needle in (local.lower(), label.lower()):
                return iri
    return token


def _cosine(a: List[float], b: List[float]) -> float:
    """Return the cosine similarity between two equal-length vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        The cosine similarity in ``[-1.0, 1.0]``. Returns ``0.0`` when either
        vector has zero magnitude (guards against a divide-by-zero for the
        degenerate all-zeros fallback vectors), which sorts such concepts last.
    """
    # dot(a, b) with a per-component multiply; zip stops at the shorter length.
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _concept_text(*, iri: str, meta: Dict[str, Any]) -> str:
    """Build the text embedded to represent an ontology concept.

    The text is the concept's human-facing label (falling back to the IRI's
    local-name when no label exists) optionally followed by its ``comment``.
    A label-only concept therefore embeds to exactly its label, which keeps
    the embedding stable and self-describing.

    Args:
        iri: The concept's full IRI (used only for the local-name fallback).
        meta: The concept's ontology metadata (``label``, optional ``comment``).

    Returns:
        The concept text: ``"<label-or-localname> [comment]"`` with no extra
        whitespace.
    """
    meta = meta or {}
    # local-name = last path/fragment segment of the IRI (mirrors resolve_to_iri).
    local_name = iri.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    label = str(meta.get("label", "")).strip()
    comment = str(meta.get("comment", "")).strip()
    # Prefer the label; fall back to the local-name when there is no label.
    head = label or local_name
    # Join the non-empty parts with a single space; strip guards trailing space.
    return " ".join(filter(None, [head, comment])).strip()


def _cached_embed(text: str) -> List[float]:
    """Embed ``text`` via ``embed_text``, memoized by the exact text string.

    Args:
        text: The concept text to embed.

    Returns:
        The embedding vector for ``text``.
    """
    if text not in _EMBED_CACHE:
        # Reference the module-level name so a monkeypatched embed_text is used.
        _EMBED_CACHE[text] = embed_text(text)
    return _EMBED_CACHE[text]


def retrieve_candidates(
    *,
    question: str,
    ontology_json: Dict[str, Any],
    lexical: List[str],
    k: int = 10,
    mode: str = "vkg",
) -> List[str]:
    """Union lexical candidates with query-time semantically-ranked concepts.

    The VKG agent's Phase-1 candidate selection is lexical-only, so it misses
    ontology concepts a question references by value/semantics (e.g. a question
    about "customers in SME" never names the ``customers`` class literally).
    This augments the lexical set with concepts ranked by the cosine similarity
    of their embedding to the question's embedding, computed at call time — there
    is no precomputed index.

    The lexical candidates are always returned first (order preserved); novel
    semantic concepts fill the remaining ``k - len(lexical)`` slots by score.
    The entire embed/rank block is fail-soft: on ANY exception (e.g. Bedrock
    unavailable) the function logs at debug and returns the lexical set unchanged
    so a degraded embedding backend never breaks candidate selection.

    Args:
        question: The natural-language question to ground.
        ontology_json: The FULL ontology, shaped as
            ``{"classes": {iri: {"label": ..., "comment": ...}}, "properties": {...}}``.
        lexical: IRIs already selected lexically. Always retained, order kept.
        k: Target total number of candidates (lexical + novel). Defaults to 10.
        mode: Reserved for per-agent behavior variation; accepted but unused.

    Returns:
        A deduplicated list of IRIs: the ``lexical`` IRIs (order preserved)
        followed by the top-scoring novel IRIs, capped at ``k`` total.
    """
    # Preserve lexical order while giving O(1) membership tests for dedup.
    result: List[str] = list(lexical)
    lexical_set = set(lexical)

    try:
        question_vec = _cached_embed(question)

        # Score every ontology concept not already selected lexically.
        scored: List[tuple[float, str]] = []
        for section in ("classes", "properties"):
            for iri, meta in (ontology_json.get(section, {}) or {}).items():
                if iri in lexical_set:
                    continue
                text = _concept_text(iri=iri, meta=meta)
                if not text:
                    continue  # nothing to embed for a label-less, comment-less concept
                concept_vec = _cached_embed(text)
                scored.append((_cosine(question_vec, concept_vec), iri))

        # Highest cosine first; take only enough to fill up to k total.
        scored.sort(key=lambda pair: pair[0], reverse=True)
        remaining = max(0, k - len(result))

        seen = set(result)
        for _score, iri in scored[:remaining]:
            if iri not in seen:  # dedup against lexical + any duplicate IRIs
                result.append(iri)
                seen.add(iri)
        return result
    except Exception:  # noqa: BLE001 — fail soft; embedding backend is best-effort
        _LOG.debug(
            "retrieve_candidates: semantic ranking failed; "
            "falling back to lexical candidates",
            exc_info=True,
        )
        return list(lexical)
