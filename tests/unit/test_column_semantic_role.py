"""Column semantic-role classification — distinguish a human-readable LABEL
column from its surrogate CODE/ID so the SQL generator groups by / returns the
meaningful column.

The 2026-06-08 review found "top 5 most common party types and their
human-readable descriptions" grouped by ``party_type_code`` (unique per row →
every count 1) instead of ``party_type`` (Organization/Trust/Individual). Both
identifiers are real, so the grounding gate cannot catch it; a semantic hint on
the slice columns lets the generator prefer the label.
"""
from __future__ import annotations

from agents.metadata_query_agent.tier2.markdown_slice_parser import (
    annotate_semantic_roles,
    classify_column_role,
)


def test_code_suffix_is_code_role() -> None:
    assert classify_column_role(name="party_type_code", sibling_names=set()) == "code"
    assert classify_column_role(name="status_cd", sibling_names=set()) == "code"
    assert classify_column_role(name="policy_id", sibling_names=set()) == "code"
    assert classify_column_role(name="rider_sk", sibling_names=set()) == "code"


def test_bare_form_paired_with_code_is_label() -> None:
    # party_type is the bare form of party_type_code present in the same table →
    # it is the human-readable label.
    siblings = {"party_type", "party_type_code", "party_id"}
    assert classify_column_role(name="party_type", sibling_names=siblings) == "label"


def test_plain_column_is_generic() -> None:
    assert classify_column_role(name="market_value", sibling_names=set()) == "generic"
    assert classify_column_role(name="full_name", sibling_names=set()) == "generic"


def test_annotate_adds_semantic_role_per_table() -> None:
    columns = [
        {"table_id": "n.party", "name": "party_id", "type": "varchar", "description": ""},
        {"table_id": "n.party", "name": "party_type", "type": "varchar", "description": ""},
        {"table_id": "n.party", "name": "party_type_code", "type": "varchar", "description": ""},
        {"table_id": "n.holding", "name": "market_value", "type": "double", "description": ""},
    ]
    out = annotate_semantic_roles(columns)
    by = {(c["table_id"], c["name"]): c["semantic_role"] for c in out}
    assert by[("n.party", "party_id")] == "code"
    assert by[("n.party", "party_type")] == "label"      # paired bare form
    assert by[("n.party", "party_type_code")] == "code"
    assert by[("n.holding", "market_value")] == "generic"
    # The original four keys are preserved (additive annotation).
    assert set(out[0].keys()) == {"table_id", "name", "type", "description",
                                  "semantic_role"}


def test_annotate_pairs_are_scoped_per_table() -> None:
    # A bare 'type' in table A must not be labelled just because table B has a
    # 'type_code' — the paired code must be in the SAME table.
    columns = [
        {"table_id": "n.a", "name": "type", "type": "varchar", "description": ""},
        {"table_id": "n.b", "name": "type_code", "type": "varchar", "description": ""},
    ]
    out = annotate_semantic_roles(columns)
    by = {(c["table_id"], c["name"]): c["semantic_role"] for c in out}
    assert by[("n.a", "type")] == "generic"   # no type_code in table a
    assert by[("n.b", "type_code")] == "code"
