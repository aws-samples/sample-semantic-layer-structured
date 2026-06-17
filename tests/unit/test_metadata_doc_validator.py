"""Tests for the source-side schema validator in metadata_agent.doc_validator.

These exercise the pure functions only — no AWS calls. They encode the exact
fabrication that caused the curated-layer query failure (holding.party_id /
holding.invest_product_id columns + a holding_subaccount.invest_product_id join
edge that does not exist) and assert it is dropped while real columns survive.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from metadata_agent.doc_validator import (  # noqa: E402
    extract_column_rows,
    extract_reference_edges,
    validate_and_clean,
)


# A document shaped like what metadata_agent writes, holding both real columns
# (market_value, holding_id, policy_id) and a hallucinated one (party_id).
_DOC = """# s3tablescatalog/bucket.normalized.holding

## Overview
One row per investment holding within a policy.

## Reference Tables
- `holding_subaccount`: JOIN holding_subaccount hs ON holding.holding_id = hs.holding_id
- `invest_product`: JOIN invest_product ip ON holding.invest_product_id = ip.invest_product_id

## Columns
| Column | Type | Description |
|--------|------|-------------|
| holding_id | string | PK of the holding. |
| policy_id | string | FK to policy. |
| market_value | double | Current market value. |
| party_id | string | Owning party (HALLUCINATED — not on this table). |

## Notes
None.
"""


def test_extract_column_rows_parses_table():
    rows = extract_column_rows(_DOC)
    names = [r["name"] for r in rows]
    assert names == ["holding_id", "policy_id", "market_value", "party_id"]
    # raw_line is carried verbatim so the rewriter can delete the exact line.
    assert any(r["raw_line"].strip().startswith("| party_id ") for r in rows)


def test_extract_reference_edges_parses_on_clause():
    edges = extract_reference_edges(_DOC)
    by_target = {e["to"]: e for e in edges}
    assert by_target["holding_subaccount"]["from_col"] == "holding_id"
    assert by_target["holding_subaccount"]["to_col"] == "holding_id"
    assert by_target["invest_product"]["from_col"] == "invest_product_id"


def test_drops_hallucinated_column():
    real = {"holding_id", "policy_id", "market_value"}  # NO party_id
    cleaned, dropped = validate_and_clean(md=_DOC, real_columns=real)
    assert "column:party_id" in dropped
    assert "party_id" not in cleaned
    # Real columns and the rest of the doc survive.
    assert "market_value" in cleaned
    assert "## Overview" in cleaned


def test_drops_join_edge_with_bad_from_col():
    # invest_product_id is not a column on holding → the invest_product join edge
    # (from_col = invest_product_id) must be dropped.
    real = {"holding_id", "policy_id", "market_value"}
    cleaned, dropped = validate_and_clean(md=_DOC, real_columns=real)
    assert any(d.startswith("join:invest_product") for d in dropped)
    assert "ip.invest_product_id" not in cleaned
    # The valid holding_subaccount join (from_col = holding_id) survives.
    assert "holding_subaccount hs ON holding.holding_id" in cleaned


def test_drops_join_edge_with_bad_to_col_when_target_known():
    doc = """## Reference Tables
- `holding_subaccount`: JOIN holding_subaccount hs ON holding.holding_id = hs.invest_product_id

## Columns
| Column | Type | Description |
|--------|------|-------------|
| holding_id | string | PK. |
"""
    real = {"holding_id"}
    # holding_subaccount really has holding_id/fundcode but NOT invest_product_id.
    targets = {"holding_subaccount": {"holding_id", "fundcode", "fundname"}}
    cleaned, dropped = validate_and_clean(
        md=doc, real_columns=real, target_columns=targets,
    )
    assert any(d.startswith("join:holding_subaccount") for d in dropped)
    assert "hs.invest_product_id" not in cleaned


def test_unknown_target_table_edge_is_kept():
    # to_col is only validated when the target schema is known. With no
    # target_columns entry, the edge is left untouched (don't guess it away).
    doc = """## Reference Tables
- `mystery_table`: JOIN mystery_table m ON holding.holding_id = m.some_col

## Columns
| Column | Type | Description |
|--------|------|-------------|
| holding_id | string | PK. |
"""
    cleaned, dropped = validate_and_clean(md=doc, real_columns={"holding_id"})
    assert dropped == []
    assert "mystery_table" in cleaned


def test_drops_reference_edge_to_table_outside_layer():
    # Regression for the participant/payout degrade: rider_participant's doc
    # references a `participant` table that was never materialized in the layer.
    # With a layer inventory that omits `participant`, the edge must be dropped so
    # the query agent never names it as a missing bridge table.
    doc = """## Reference Tables
- `holding`: JOIN holding h ON rider_participant.holding_id = h.holding_id
- `participant`: JOIN participant p ON rider_participant.participant_sk = p.participant_sk

## Columns
| Column | Type | Description |
|--------|------|-------------|
| holding_id | string | FK to holding. |
| participant_sk | string | Surrogate key of the participant. |
"""
    real = {"holding_id", "participant_sk"}
    layer = {"holding", "rider_participant", "rider", "party"}  # NO participant
    cleaned, dropped = validate_and_clean(
        md=doc, real_columns=real, layer_tables=layer,
    )
    assert "join:participant(not-in-layer)" in dropped
    assert "participant p ON" not in cleaned
    # The in-layer holding join survives untouched.
    assert "holding h ON rider_participant.holding_id" in cleaned


def test_layer_membership_not_checked_when_inventory_absent():
    # Back-compat: with no layer_tables supplied, target-table membership is not
    # enforced (an out-of-layer-looking edge with valid columns is kept).
    doc = """## Reference Tables
- `participant`: JOIN participant p ON rider_participant.participant_sk = p.participant_sk

## Columns
| Column | Type | Description |
|--------|------|-------------|
| participant_sk | string | Surrogate key. |
"""
    cleaned, dropped = validate_and_clean(md=doc, real_columns={"participant_sk"})
    assert dropped == []
    assert "participant" in cleaned


def test_in_layer_reference_edge_kept():
    # An edge whose target IS in the layer and whose from_col is real survives.
    doc = """## Reference Tables
- `holding`: JOIN holding h ON rider_participant.holding_id = h.holding_id

## Columns
| Column | Type | Description |
|--------|------|-------------|
| holding_id | string | FK to holding. |
"""
    cleaned, dropped = validate_and_clean(
        md=doc, real_columns={"holding_id"},
        layer_tables={"holding", "rider_participant"},
    )
    assert dropped == []
    assert cleaned == doc


def test_case_insensitive_no_false_drop():
    # Doc uses a different CASE of the same identifier (MARKET_VALUE / Holding_Id);
    # catalog returns market_value / holding_id. Same characters, different case →
    # must NOT be dropped. A false drop of a real column is worse than the bug.
    # (Note: an underscore/spelling difference like `MarketValue` vs `market_value`
    # is a genuinely different identifier and IS correctly dropped — downstream
    # grounding matches exact normalized names.)
    doc = """## Columns
| Column | Type | Description |
|--------|------|-------------|
| MARKET_VALUE | double | Current value. |
| Holding_Id | string | PK. |
"""
    cleaned, dropped = validate_and_clean(
        md=doc, real_columns={"market_value", "holding_id"},
    )
    assert dropped == []
    assert "MARKET_VALUE" in cleaned


def test_empty_real_columns_skips_validation():
    # An unresolved schema (empty set) disables validation entirely — the save
    # must never be blocked on an infra failure.
    cleaned, dropped = validate_and_clean(md=_DOC, real_columns=set())
    assert dropped == []
    assert cleaned == _DOC


def test_clean_doc_passes_through_unchanged():
    real = {"holding_id", "policy_id", "market_value", "party_id"}
    targets = {
        "holding_subaccount": {"holding_id"},
        "invest_product": {"invest_product_id"},
    }
    # invest_product_id is still not a column on holding, so that edge drops; use a
    # doc whose every reference is valid to assert byte-for-byte passthrough.
    doc = """## Reference Tables
- `holding_subaccount`: JOIN holding_subaccount hs ON holding.holding_id = hs.holding_id

## Columns
| Column | Type | Description |
|--------|------|-------------|
| holding_id | string | PK. |
| market_value | double | Value. |
"""
    cleaned, dropped = validate_and_clean(
        md=doc, real_columns=real, target_columns=targets,
    )
    assert dropped == []
    assert cleaned == doc
