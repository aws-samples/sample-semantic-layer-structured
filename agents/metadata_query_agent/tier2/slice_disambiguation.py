"""Phase 3b (RAG): slice-level disambiguation guard.

Phase 2 disambiguation is *term-level* and runs against the Phase 1 candidates
before the slice exists — it only catches "this word maps to >1 table." But the
Phase 3 judge-expand loop and the Phase 5 grounding back-edge both *grow* the
slice afterward, which can introduce ambiguity Phase 2 never saw:

  * a needed column name present on >1 slice table (which table's column?),
  * >1 viable JOIN path between the tables the question needs,
  * a question literal that could bind to a filter column on >1 table.

Placed on the ``Phase 3 → Phase 4`` edge, this guard re-runs on the initial
slice AND on every re-expansion, so ambiguity introduced by a widened slice is
still caught before SQL generation. It resolves heuristically where the slice's
own join graph disambiguates, and only escalates genuinely unresolvable cases to
a ``needs_clarification`` payload (reusing Phase 2's builder for an identical
frontend shape).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from agents.shared.disambiguation_common import (
        _query_terms,
        inflection_variants,
    )
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.disambiguation_common import (  # type: ignore
        _query_terms,
        inflection_variants,
    )


# --- Role vocabulary: DERIVED from the curated slice, not hard-coded -----------
#
# The policy-role vocabulary (which words name a role, which words are synonyms
# of the same role, and the tokens that evidence a role) is derived at runtime by
# ``_role_vocabulary`` rather than hard-coded, keeping layer-specific knowledge out
# of the agent. It parses the role enumeration the metadata agent already authors
# into the curated ``columns[].description`` text (the ``Values: …`` / ``Role values
# include: …`` convention, with an optional ``(synonyms: …)`` hint).
#
# Convention parsed (case-insensitive), e.g.::
#
#     Role of the party on the policy. Values: Owner (synonyms: Policyholder),
#     Insured, Beneficiary. Each value is a distinct policy party-role.
#
# yields canonical groups ``{owner, insured, beneficiary}`` and a word→group map
# ``{owner: owner, policyholder: owner, insured: insured, beneficiary: …}``.
#
# When NO slice column declares such an enumeration the vocabulary is empty and
# the unsupported-relationship guard degrades to a no-op (see
# ``detect_unsupported_relationship`` / design §4c): absent supporting metadata we
# do NOT invent a domain-specific fast-fail.

# Matches the enumeration lead-in followed by the value list, capturing the list
# body up to the sentence terminator. Accepts both the bare ``Values:`` form and
# the longer ``Role values include:`` form the B1 enrichment authored.
_ENUM_LEAD_IN = re.compile(
    r"(?:role\s+values?(?:\s+include)?|values?)\s*:\s*",
    re.IGNORECASE,
)
# Matches one value entry: a label optionally followed by ``(synonyms: a, b)``.
# The label is everything up to an opening paren or a comma; synonyms are the
# comma-separated tokens inside the parenthetical.
_VALUE_ENTRY = re.compile(
    r"(?P<label>[^,()]+?)\s*"
    r"(?:\(\s*synonyms?\s*:\s*(?P<synonyms>[^)]*)\))?\s*(?:,|$)",
    re.IGNORECASE,
)


def _columns_by_name(slice_obj: Dict[str, Any]) -> Dict[str, List[str]]:
    """Return ``{column_name_lower: [table_id, ...]}`` from the slice columns."""
    by_name: Dict[str, List[str]] = {}
    for col in slice_obj.get("columns", []) or []:
        if not isinstance(col, dict):
            continue
        name = (col.get("name") or "").lower()
        tid = col.get("table_id") or ""
        if not name or not tid:
            continue
        by_name.setdefault(name, [])
        if tid not in by_name[name]:
            by_name[name].append(tid)
    return by_name


def _join_adjacency(slice_obj: Dict[str, Any]) -> Dict[str, set]:
    """Build an undirected table adjacency map from the slice ``joins``."""
    adj: Dict[str, set] = {}
    for join in slice_obj.get("joins", []) or []:
        if not isinstance(join, dict):
            continue
        a, b = join.get("from"), join.get("to")
        if not a or not b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return adj


def find_slice_ambiguities(*, question: str, slice_obj: Dict[str, Any]
                           ) -> Dict[str, Any]:
    """Detect slice-level ambiguity a term-level pass cannot see.

    Args:
        question: The natural-language user question.
        slice_obj: The parsed Phase 3 slice (``tables``/``columns``/``joins``).

    Returns:
        ``{"ambiguous": bool, "items": [...], "resolved": {term: table_id}}``.
        ``items`` lists unresolved collisions (each with ``term`` + ``matches``
        of ``{table, database, column}``); ``resolved`` records collisions the
        join graph disambiguated so the Phase 4 prompt can pin the binding.
    """
    terms = set(_query_terms(question))
    by_name = _columns_by_name(slice_obj)
    adj = _join_adjacency(slice_obj)
    tables = list(slice_obj.get("tables", []) or [])

    items: List[Dict[str, Any]] = []
    resolved: Dict[str, str] = {}

    for term in terms:
        owners = by_name.get(term, [])
        if len(owners) <= 1:
            continue  # not a collision (0 or 1 owning table)

        # Heuristic resolution: if exactly one owner is connected in the slice
        # join graph (i.e. reachable / has any join edge), prefer it. A lone
        # connected table among otherwise-isolated ones is the intended anchor.
        connected = [t for t in owners if adj.get(t)]
        if len(connected) == 1:
            resolved[term] = connected[0]
            continue

        # Otherwise it's a genuine collision — surface for clarification.
        matches: List[Dict[str, str]] = []
        for tid in owners:
            database, table = (tid.split(".", 1) if "." in tid else ("", tid))
            matches.append({"table": table, "database": database, "column": term})
        items.append({"term": term, "matches": matches})

    return {"ambiguous": bool(items), "items": items, "resolved": resolved}


def _normalize_role_word(word: str) -> str:
    """Collapse a role label/word to a comparison key.

    Lower-cases, strips surrounding whitespace, and removes internal spaces so a
    spaced phrase and its closed compound collapse to the SAME key (``policy
    holder`` → ``policyholder``). This is what lets the derived synonym list cover
    the spacing variant the old code special-cased with a literal
    ``if "policy holder" in q``.
    """
    return re.sub(r"\s+", "", (word or "").strip().lower())


def _role_vocabulary(
    slice_obj: Dict[str, Any]
) -> Tuple[Dict[str, str], Dict[str, Set[str]]]:
    """Derive the policy-role vocabulary from the slice column descriptions.

    Scans every ``columns[].description`` for the metadata-agent's role
    enumeration convention (``Values: A (synonyms: B, C), D`` /
    ``Role values include: …``) and returns the runtime equivalents of the two
    deleted module constants:

      * ``word_to_group`` — ``{normalized_word: canonical_group}``. Each canonical
        value is its own group; every synonym maps to that same group. Both forms
        are normalized via :func:`_normalize_role_word` AND expanded across number
        inflection (``owners`` → ``owner``) so question words match regardless of
        plurality or spacing.
      * ``group_tokens`` — ``{canonical_group: {evidence_tokens}}``. The tokens
        searched for in column name/description text as evidence that the slice can
        represent a group; the canonical label plus all its synonyms.

    Returns two empty dicts when NO column declares a role enumeration — the
    signal :func:`detect_unsupported_relationship` uses to become a no-op (design
    §4c): absent supporting metadata, do not invent a domain-specific fast-fail.

    Args:
        slice_obj: The parsed Phase 3 slice (``tables``/``columns``/``joins``).

    Returns:
        ``(word_to_group, group_tokens)`` derived from the slice metadata.
    """
    word_to_group: Dict[str, str] = {}
    group_tokens: Dict[str, Set[str]] = {}

    for col in slice_obj.get("columns", []) or []:
        if not isinstance(col, dict):
            continue
        desc = col.get("description") or ""
        if not desc:
            continue
        lead = _ENUM_LEAD_IN.search(desc)
        if not lead:
            continue
        # Take the enumeration body up to the first sentence terminator after the
        # lead-in. A trailing ``.`` ends the value list (the convention writes a
        # clarifying sentence after it, e.g. "Each value is a distinct …").
        body = desc[lead.end():]
        body = re.split(r"[.;]", body, maxsplit=1)[0]
        for entry in _VALUE_ENTRY.finditer(body):
            label = (entry.group("label") or "").strip()
            canonical = _normalize_role_word(label)
            if not canonical:
                continue
            synonyms_raw = entry.group("synonyms") or ""
            synonyms = [s for s in (
                _normalize_role_word(s) for s in synonyms_raw.split(",")
            ) if s]
            # The canonical value names its own group; record the label + every
            # synonym as a question word mapping to that group, and as an evidence
            # token for representability.
            tokens = group_tokens.setdefault(canonical, set())
            tokens.add(canonical)
            for word in [canonical, *synonyms]:
                # Map the word and its number inflections to the group so a plural
                # question word ("policyholders") still resolves.
                for variant in inflection_variants(word) | {word}:
                    word_to_group[variant] = canonical
                tokens.add(word)

    return word_to_group, group_tokens


def _representable_role_groups(
    slice_obj: Dict[str, Any], group_tokens: Dict[str, Set[str]]
) -> Set[str]:
    """Return the role GROUPS the slice can demonstrably represent.

    Evidence is read from column metadata only — NO data scan. A group is
    representable when some column NAME or DESCRIPTION contains one of the group's
    evidence tokens (the canonical label or a synonym derived by
    :func:`_role_vocabulary`). An ``Owner`` enumeration value or an
    ``owner_party_id`` column both count.

    A column merely named ``*role*`` is intentionally NOT treated as generic
    evidence: the curated ``relation.relationship_role`` (Primary/Secondary) is an
    interpersonal role, not a policy party-role, so trusting any ``role`` column
    would mask the exact gap this guard targets. Specific token evidence — drawn
    from the slice's own enumeration — is required.

    Args:
        slice_obj: The parsed Phase 3 slice (``tables``/``columns``/``joins``).
        group_tokens: ``{group: {evidence_tokens}}`` from :func:`_role_vocabulary`.

    Returns:
        The subset of ``group_tokens`` keys the slice can represent.
    """
    groups: Set[str] = set()
    for col in slice_obj.get("columns", []) or []:
        if not isinstance(col, dict):
            continue
        raw = f"{(col.get('name') or '')} {(col.get('description') or '')}".lower()
        # Search BOTH the raw lower-cased text and a space-normalized form so a
        # space-stripped evidence token ("policyholder") still matches a spaced
        # column phrase ("policy holder"). Normalizing the haystack can only ADD
        # matches, biasing toward "representable" — the safe direction, since a
        # FALSE gap would be a spurious fast-fail (the thing design §4c forbids).
        norm = _normalize_role_word(raw)
        for group, tokens in group_tokens.items():
            if any(tok in raw or tok in norm for tok in tokens):
                groups.add(group)
    return groups


def _referenced_role_groups(
    question: str, word_to_group: Dict[str, str]
) -> Set[str]:
    """Return the distinct role GROUPS ``question`` references via the vocabulary.

    Matches each significant question term to a derived role group (number
    inflection / spacing already folded into the vocabulary keys), then also scans
    the space-normalized question for any multi-word synonym the per-term
    tokenizer would have split (e.g. a synonym authored as ``policy holder``).
    """
    q = (question or "").lower()
    referenced: Set[str] = set()
    for term in _query_terms(q):
        group = word_to_group.get(_normalize_role_word(term))
        if group:
            referenced.add(group)
    q_norm = _normalize_role_word(q)
    for word, group in word_to_group.items():
        if word in q_norm:
            referenced.add(group)
    return referenced


def _unsupported_reason(
    *, missing_groups: List[str], other_groups: List[str], tables: List[str]
) -> str:
    """Build the user-facing degrade reason from derived role labels + tables.

    Generated from the missing role label(s), the comparison role(s), and the
    slice tables scanned — no hard-coded, insurance-specific prose literal.

    Args:
        missing_groups: Referenced role groups absent from the slice.
        other_groups: The remaining referenced role group(s) to compare against.
        tables: The slice table_ids scanned (for the schema-in-scope phrasing).
    """
    scope = ", ".join(sorted(tables)) or "in scope"
    missing_str = ", ".join(missing_groups)
    against = (
        f" to compare against the {', '.join(other_groups)} role"
        if other_groups else ""
    )
    return (
        f"The schema in scope ({scope}) has no column representing the "
        f"{missing_str} role{against}, so this question can't be answered with "
        "the current data."
    )


def detect_unsupported_relationship(*, question: str, slice_obj: Dict[str, Any]
                                    ) -> Optional[str]:
    """Return a user-facing reason when the question needs a per-entity role the
    slice cannot represent; otherwise ``None``.

    Targets the modeling gap behind session ``e7253c91``: "policies where the
    insured party is also the policyholder" requires comparing two DISTINCT roles
    (e.g. Insured and Owner/Policyholder) on the SAME parent entity.

    The role vocabulary is **derived from the slice itself** (see
    :func:`_role_vocabulary`), not hard-coded: which words name a role, which are
    synonyms, and the evidence tokens all come from the curated
    ``columns[].description`` enumeration the metadata agent authors.

    Deliberately CONSERVATIVE to avoid pre-empting answerable questions. It fires
    ONLY when ALL hold:
      0. some slice column DECLARES a role enumeration (else the vocabulary is
         empty → return ``None``; absent metadata we do not invent a fast-fail), AND
      1. the question references **two or more distinct** role GROUPS (the
         same-entity role COMPARISON case), AND
      2. **at least one** referenced group has no representation in the slice.

    NOTE ON REACHABILITY (de-layering consequence): because the vocabulary AND the
    representability evidence are both derived from the SAME ``columns[].description``
    enumeration, any role the question can *reference* is necessarily *representable*
    — so condition (2) cannot hold for a slice whose only role evidence is that
    enumeration. With B1 deployed, gt-00 ("insured is also the policyholder")
    therefore proceeds to the ``life_participant`` self-join rather than fast-failing;
    with no enumeration declared it is a no-op. The (1)+(2) branch below is retained
    as a CORRECT defensive path that would fire only if a future representability
    evidence channel (e.g. column-name tokens, or ``businessConcepts``) diverged
    from the enumeration prose. The grounding gate is the backstop in all cases.

    Args:
        question: The natural-language user question.
        slice_obj: The parsed Phase 3 slice.

    Returns:
        A user-facing explanation string when unsupported, else ``None``.
    """
    word_to_group, group_tokens = _role_vocabulary(slice_obj)
    if not word_to_group:
        return None  # no role enumeration declared — do not invent a fast-fail

    referenced = _referenced_role_groups(question, word_to_group)
    if len(referenced) < 2:
        return None  # not a same-entity role comparison — let Phase 4 try

    representable = _representable_role_groups(slice_obj, group_tokens)
    missing_groups = sorted(referenced - representable)
    if not missing_groups:
        return None  # every referenced role is representable — do not fast-fail

    return _unsupported_reason(
        missing_groups=missing_groups,
        other_groups=sorted(referenced - set(missing_groups)),
        tables=list(slice_obj.get("tables", []) or []),
    )


def parse_slice_obj(slice_text: str) -> Dict[str, Any]:
    """Parse the serialized slice JSON, returning ``{}`` on any failure."""
    try:
        return json.loads(slice_text) if slice_text else {}
    except (json.JSONDecodeError, TypeError):
        return {}
