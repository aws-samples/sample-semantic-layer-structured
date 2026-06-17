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
from typing import Any, Dict, List, Optional

try:
    from agents.shared.disambiguation_common import _query_terms
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared.disambiguation_common import _query_terms  # type: ignore


# Canonical policy-role GROUPS. A question word maps to a group; the group's
# member tokens are the synonyms we search for as evidence the slice can represent
# that role. Policyholder and owner are the same concept in insurance, so they
# share a group. Comparing two DISTINCT groups (e.g. insured vs owner) on the same
# policy needs a per-policy party-role representation the curated model may lack.
_ROLE_WORD_TO_GROUP: Dict[str, str] = {
    "policyholder": "owner",
    "policyholders": "owner",
    "owner": "owner",
    "owners": "owner",
    "insured": "insured",
}

# Member tokens to search for (in column names / descriptions) as evidence that a
# given role group is representable by the slice.
_ROLE_GROUP_TOKENS: Dict[str, set] = {
    "owner": {"owner", "policyholder"},
    "insured": {"insured"},
}


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


def _representable_role_groups(slice_obj: Dict[str, Any]) -> set:
    """Return the role GROUPS the slice can demonstrably represent.

    Evidence is read from column metadata only — NO data scan. A group is
    representable when some column NAME or DESCRIPTION contains one of the group's
    member tokens (the metadata-agent writes enumerations as ``Values: Owner,
    Insured, ...`` and the slice carries that description verbatim, so an Owner/
    Insured value or an ``owner_party_id`` column both count).

    A column merely named ``*role*`` is intentionally NOT treated as generic
    evidence: the curated ``relation.relationship_role`` (Primary/Secondary) is an
    interpersonal role, not a policy party-role, so trusting any ``role`` column
    would mask the exact gap this guard targets. Specific token evidence is
    required.

    Args:
        slice_obj: The parsed Phase 3 slice (``tables``/``columns``/``joins``).

    Returns:
        The subset of ``{"owner", "insured"}`` the slice can represent.
    """
    groups: set = set()
    for col in slice_obj.get("columns", []) or []:
        if not isinstance(col, dict):
            continue
        text = f"{(col.get('name') or '')} {(col.get('description') or '')}".lower()
        for group, tokens in _ROLE_GROUP_TOKENS.items():
            if any(tok in text for tok in tokens):
                groups.add(group)
    return groups


def detect_unsupported_relationship(*, question: str, slice_obj: Dict[str, Any]
                                    ) -> Optional[str]:
    """Return a user-facing reason when the question needs a per-policy party role
    the slice cannot represent; otherwise ``None``.

    Targets the modeling gap behind session ``e7253c91``: "policies where the
    insured party is also the policyholder" requires comparing an Insured-role and an
    Owner/Policyholder-role party on the SAME policy, but the curated ``relation``
    table is party-to-party with interpersonal roles only (no Insured/Owner) and no
    other table carries a policy party-role. Phase 4 then invents
    ``relation.relation_role_code = 'Insured'/'Owner'`` and the grounding gate
    degrades — a wasted generate + 2 grounding rounds.

    Deliberately CONSERVATIVE to avoid pre-empting answerable questions. It fires
    ONLY when BOTH hold:
      1. the question references **two or more distinct** policy-role GROUPS (the
         same-policy role COMPARISON case — e.g. insured AND owner/policyholder), AND
      2. **at least one** referenced group has no representation in the slice (no
         column name/description naming that role).

    A single-role question, or full role representation in the slice, returns
    ``None`` and lets Phase 4 proceed — the grounding gate remains the backstop.

    Args:
        question: The natural-language user question.
        slice_obj: The parsed Phase 3 slice.

    Returns:
        A user-facing explanation string when unsupported, else ``None``.
    """
    q = (question or "").lower()
    # Distinct role GROUPS the question references. Substring match so
    # "policyholder" / "owner" / "insured" all count; "policy holder" (spaced) too.
    referenced = {group for word, group in _ROLE_WORD_TO_GROUP.items() if word in q}
    if "policy holder" in q:
        referenced.add("owner")
    if len(referenced) < 2:
        return None  # not a same-policy role comparison — let Phase 4 try

    representable = _representable_role_groups(slice_obj)
    missing_groups = referenced - representable
    if not missing_groups:
        return None  # every referenced role is representable — do not fast-fail

    return (
        "This data model records party relationships, but it has no policyholder / "
        "owner role on a policy to compare against the insured party — so this "
        "question can't be answered with the current schema."
    )


def parse_slice_obj(slice_text: str) -> Dict[str, Any]:
    """Parse the serialized slice JSON, returning ``{}`` on any failure."""
    try:
        return json.loads(slice_text) if slice_text else {}
    except (json.JSONDecodeError, TypeError):
        return {}
