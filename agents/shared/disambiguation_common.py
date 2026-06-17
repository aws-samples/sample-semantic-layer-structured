"""Mode-agnostic disambiguation helpers shared by both query agents.

``_query_terms`` (significant-term tokenization) and ``build_clarification``
(the ``needs_clarification`` frontend payload) were first written inside the
RAG agent's ``metadata_query_agent/tier2/disambiguation.py``. The VKG agent's
Phase 2 (term→IRI) and Phase 3b (slice collision) guards need the identical
tokenization and the byte-identical clarification shape, so they live here and
both agents import them. Keeping a single ``build_clarification`` guarantees the
clarification payload is byte-identical across RAG and VKG.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

# Question-word / filler stop-words removed before matching terms to tables /
# classes. Shared so RAG (Phase 2/3b) and VKG (Phase 2/3b) agree on which words
# the question actually references.
_STOP_WORDS = {
    'how', 'many', 'what', 'which', 'who', 'where', 'when', 'why',
    'show', 'me', 'get', 'find', 'list', 'give', 'tell', 'count',
    'are', 'is', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
    'do', 'does', 'did', 'can', 'could', 'would', 'should', 'will',
    'all', 'the', 'a', 'an', 'any', 'some', 'each', 'every',
    'this', 'that', 'these', 'those',
    'from', 'with', 'their', 'for', 'in', 'on', 'at', 'to', 'of',
    'and', 'or', 'not', 'no', 'by', 'as', 'per',
    'there', 'total', 'number', 'records', 'entries', 'items',
    'please', 'just', 'only', 'top', 'first', 'last', 'latest',
}


def inflection_variants(word: str) -> set:
    """Return ``word`` plus its simple English singular/plural inflections.

    Used to match a question term against a table / class name across number
    (e.g. ``parties`` ↔ ``party``, ``policies`` ↔ ``policy``, ``addresses`` ↔
    ``address``). A naive ``rstrip('s')`` is wrong for the two common irregular
    endings: ``parties`` → ``partie`` (should be ``party``) and ``boxes`` →
    ``boxe`` (should be ``box``). This generates variants in BOTH directions so
    a singular term matches a plural name and vice versa:

      * ``-y`` → ``-ies``   and ``-ies`` → ``-y``   (party ↔ parties)
      * sibilant + ``-es``  and ``-es`` → bare       (address ↔ addresses, box ↔ boxes)
      * ``-s`` → bare       and bare → ``-s``        (table ↔ tables)

    All variants are lower-cased and the original word is always included, so an
    exact match still succeeds. Intentionally lexical-only (no irregular plurals
    like person/people): it covers the snake_case table/class-name space, where
    names are regular nouns.

    Args:
        word: A single query term or name token.

    Returns:
        The set of candidate forms to test for an exact name match.
    """
    w = (word or "").strip().lower()
    if not w:
        return set()
    variants = {w}
    # Pluralizing direction — term is singular, the name might be plural.
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        variants.add(w[:-1] + "ies")  # party -> parties
    if w.endswith(("s", "x", "z", "ch", "sh")):
        variants.add(w + "es")        # address -> addresses, box -> boxes
    variants.add(w + "s")             # table -> tables
    # Singularizing direction — term is plural, the name might be singular.
    if w.endswith("ies") and len(w) > 3:
        variants.add(w[:-3] + "y")    # parties -> party
    if w.endswith("es") and len(w) > 2:
        variants.add(w[:-2])          # addresses -> address, boxes -> box
    if w.endswith("s") and len(w) > 1:
        variants.add(w[:-1])          # tables -> table
    return variants


def _query_terms(question: str) -> List[str]:
    """Return the significant lower-cased terms in ``question``.

    Drops stop-words and short tokens. Shared by both agents' Phase 2 and
    Phase 3b so they agree on which words the question actually references.

    Args:
        question: The natural-language user question.
    """
    words = re.findall(r'\b\w+\b', question.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 2]


def build_clarification(*, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the ``needs_clarification`` payload from ambiguity ``items``.

    The shape is the frontend contract for a clarification prompt::

        {"needs_clarification": true,
         "clarification_question": "...",
         "options": [{"id": "...", "label": "..."}]}

    Args:
        items: Each item has a ``term`` and a ``matches`` list of
            ``{table, database}`` (and optional ``column``) dicts.
    """
    # Flatten the competing interpretations across all ambiguous items into a
    # single options list; the frontend renders these as selectable chips.
    options: List[Dict[str, str]] = []
    seen: set = set()
    terms: List[str] = []
    for item in items:
        term = item.get('term', '')
        if term:
            terms.append(term)
        for match in item.get('matches', []):
            table = match.get('table', '')
            database = match.get('database', '')
            column = match.get('column', '')
            opt_id = table if not column else f"{table}.{column}"
            if opt_id in seen:
                continue
            seen.add(opt_id)
            # Prefer an explicit human-readable label when provided (VKG passes
            # the full class IRI as the option id but a readable local name as
            # the label, so the user sees "party" while the resolution can seed
            # the real IRI). Fall back to the id-derived label otherwise.
            label = match.get('label') or (
                f"{table}" + (f".{column}" if column else ""))
            if database:
                label = f"{label} (database: {database})"
            options.append({'id': opt_id, 'label': label})

    if terms:
        joined = "', '".join(terms)
        question = (
            f"Which interpretation of '{joined}' do you mean?"
            if len(terms) == 1
            else f"Could you clarify which interpretation you mean for: '{joined}'?"
        )
    else:
        question = "Could you clarify your request?"

    return {
        'needs_clarification': True,
        'clarification_question': question,
        'options': options,
        # The ambiguous term(s) this clarification is about — threaded into the
        # pending record so that, once resolved, the agent can persist a crisp
        # "<term> → <chosen target>" lesson into AgentCore Memory.
        'terms': terms,
    }


def build_clarification_from_options(
    *, options: List[Dict[str, str]], terms: List[str],
) -> Dict[str, Any]:
    """Rebuild a ``needs_clarification`` payload from PREVIOUSLY-offered options.

    When a low-confidence clarification (one with no specific ambiguous term —
    its options are just "the top candidate tables for this question") re-fires
    on a later turn, the candidate list is re-derived from a FRESH, non-
    deterministic KB retrieval, so the user sees a *different* set of options
    every turn and can never converge (the "different 5 each turn" churn in
    session 4c8a50c7). Reusing the options the user was already shown keeps the
    target stable across the re-ask.

    Produces the same payload shape as :func:`build_clarification` so the
    frontend contract is unchanged. The options are passed through verbatim
    (already ``{id, label}``), so no reverse-parsing of ``table.column`` ids is
    needed.

    Args:
        options: The ``[{id, label}]`` list carried on the prior turn's pending
            clarification record.
        terms: The ambiguous term(s) the prior clarification was about — reused
            so the question text and persisted lesson key stay identical.
    """
    clean: List[Dict[str, str]] = []
    seen: set = set()
    for opt in options:
        if not isinstance(opt, dict):
            continue
        opt_id = opt.get('id') or ''
        if not opt_id or opt_id in seen:
            continue
        seen.add(opt_id)
        clean.append({'id': opt_id, 'label': opt.get('label') or opt_id})

    clean_terms = [t for t in (terms or []) if isinstance(t, str) and t]
    if clean_terms:
        joined = "', '".join(clean_terms)
        question = (
            f"Which interpretation of '{joined}' do you mean?"
            if len(clean_terms) == 1
            else f"Could you clarify which interpretation you mean for: '{joined}'?"
        )
    else:
        question = "Could you clarify your request?"

    return {
        'needs_clarification': True,
        'clarification_question': question,
        'options': clean,
        'terms': clean_terms,
    }
