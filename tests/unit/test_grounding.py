"""Unit tests for the Phase 5 grounding gate (tier2/grounding.py)."""
import json

from agents.metadata_query_agent.tier2.grounding import (
    build_grounding_feedback,
    check_grounding,
    extract_sql_identifiers,
)


def _slice(tables, columns, joins=None):
    return json.dumps({
        "tables": tables,
        "columns": [{"table_id": t, "name": c} for t, c in columns],
        "joins": joins or [],
    })


def test_extract_identifiers_tables_and_columns():
    ids = extract_sql_identifiers(
        "SELECT id, region FROM customers WHERE region = 'x'", dialect="athena")
    assert "customers" in ids["tables"]
    assert {"id", "region"} <= ids["columns"]


def test_fully_grounded_returns_empty():
    slice_text = _slice(["db.customers"], [("db.customers", "id"),
                                           ("db.customers", "region")])
    missing = check_grounding(
        sql="SELECT id, region FROM customers", slice_text=slice_text,
        dialect="athena")
    assert missing == []


def test_invented_column_is_flagged():
    slice_text = _slice(["db.customers"], [("db.customers", "id")])
    missing = check_grounding(
        sql="SELECT id, ssn FROM customers", slice_text=slice_text,
        dialect="athena")
    assert "column:ssn" in missing


def test_invented_table_is_flagged():
    slice_text = _slice(["db.customers"], [("db.customers", "id")])
    missing = check_grounding(
        sql="SELECT id FROM orders", slice_text=slice_text, dialect="athena")
    assert "table:orders" in missing


def test_bare_table_matches_qualified_slice_table():
    slice_text = _slice(["db.customers"], [("db.customers", "id")])
    # SQL uses the bare table name; slice records db.customers — must still ground.
    missing = check_grounding(
        sql="SELECT id FROM customers", slice_text=slice_text, dialect="athena")
    assert missing == []


def test_user_literal_constant_not_flagged():
    slice_text = _slice(["db.customers"], [("db.customers", "status")])
    missing = check_grounding(
        sql="SELECT status FROM customers WHERE status = 'active'",
        slice_text=slice_text, dialect="athena")
    # 'active' is a string literal, not a column reference → not flagged.
    assert missing == []


def test_join_key_from_slice_joins_grounds():
    slice_text = json.dumps({
        "tables": ["db.a", "db.b"],
        "columns": [{"table_id": "db.a", "name": "id"}],
        "joins": [{"from": "db.a", "to": "db.b",
                   "from_col": "b_id", "to_col": "id"}],
    })
    # b_id only appears as a join key, not in columns — should still ground.
    missing = check_grounding(
        sql="SELECT a.id FROM a JOIN b ON a.b_id = b.id",
        slice_text=slice_text, dialect="athena")
    assert missing == []


def test_qualified_column_does_not_ground_against_other_slice_table():
    """Regression: ph.is_primary must NOT ground just because a DIFFERENT slice
    table (relation) has an is_primary column. The qualified reference must be
    checked against the table the alias resolves to (phone)."""
    slice_text = json.dumps({
        "tables": ["normalized.party", "normalized.phone", "normalized.relation"],
        "columns": [
            {"table_id": "normalized.phone", "name": "party_id"},
            {"table_id": "normalized.phone", "name": "phone_type_code"},
            {"table_id": "normalized.party", "name": "party_id"},
            {"table_id": "normalized.party", "name": "party_status"},
            # is_primary exists ONLY on relation, NOT on phone:
            {"table_id": "normalized.relation", "name": "is_primary"},
        ],
        "joins": [],
    })
    sql = (
        "SELECT COUNT(p.party_id) FROM normalized.party p "
        "JOIN normalized.phone ph ON ph.party_id = p.party_id "
        "WHERE p.party_status = 'Active' AND ph.is_primary = true"
    )
    missing = check_grounding(sql=sql, slice_text=slice_text, dialect="athena")
    assert "column:phone.is_primary" in missing


def test_qualified_column_grounds_against_its_own_table():
    """The mirror case: a qualified column that DOES exist on its table grounds."""
    slice_text = json.dumps({
        "tables": ["normalized.phone"],
        "columns": [{"table_id": "normalized.phone", "name": "phone_type_code"}],
        "joins": [],
    })
    missing = check_grounding(
        sql="SELECT ph.phone_type_code FROM normalized.phone ph",
        slice_text=slice_text, dialect="athena")
    assert missing == []


def test_select_alias_in_order_by_is_not_flagged():
    """Regression: a SELECT output alias (``COUNT(*) AS party_count``) re-used in
    ORDER BY/GROUP BY parses as an exp.Column but is NOT a schema column — it
    must not be grounding-checked. Previously `party_count` was falsely flagged
    ungrounded, degrading an otherwise-valid aggregate query."""
    slice_text = _slice(["normalized.party"], [("normalized.party", "party_type")])
    sql = (
        "SELECT party_type, COUNT(*) AS party_count "
        "FROM normalized.party "
        "GROUP BY party_type "
        "ORDER BY party_count DESC LIMIT 5"
    )
    # The alias must not appear in extracted columns at all.
    ids = extract_sql_identifiers(sql, dialect="athena")
    assert "party_count" not in ids["columns"]
    # And the query grounds fully (party_type is in the slice; the alias is ignored).
    assert check_grounding(sql=sql, slice_text=slice_text, dialect="athena") == []


def test_qualified_reference_matching_an_alias_is_still_checked():
    """A QUALIFIED reference like ``t.party_count`` is a real column ref, not the
    output alias — so it must still be grounding-checked (and flagged when absent)."""
    slice_text = _slice(["normalized.party"], [("normalized.party", "party_type")])
    sql = (
        "SELECT COUNT(*) AS party_count, p.party_count "
        "FROM normalized.party p GROUP BY p.party_count"
    )
    missing = check_grounding(sql=sql, slice_text=slice_text, dialect="athena")
    assert "column:party.party_count" in missing


def test_qualified_column_on_columnless_slice_table_is_flagged():
    """Regression (gt-row-04 blind spot): a table listed in slice `tables` but
    contributing ZERO parsed columns (the budget fitter dropped them, or the
    table carried only audit cols) must NOT silently ground a column qualified to
    it. holding_payout is present in `tables` with no `columns` entry, so the
    hallucinated hp.payout_frequency must be flagged rather than skipped — this is
    exactly the column that previously slipped the gate and reached Athena."""
    slice_text = json.dumps({
        "tables": ["normalized.holding", "normalized.holding_payout"],
        # holding_payout appears in tables but has NO columns entries:
        "columns": [{"table_id": "normalized.holding", "name": "holding_id"}],
        "joins": [],
    })
    sql = (
        "SELECT hp.payout_frequency FROM normalized.holding h "
        "JOIN normalized.holding_payout hp ON hp.holding_id = h.holding_id"
    )
    missing = check_grounding(sql=sql, slice_text=slice_text, dialect="athena")
    assert "column:holding_payout.payout_frequency" in missing


def test_columnless_slice_table_falls_back_to_slicewide_membership():
    """Bounds the gt-row-04 fix against false positives: a column qualified to a
    column-less slice table still grounds when the column name appears somewhere
    in the slice (here holding_id, a real join key). The all_columns fallback
    keeps the new branch no stricter than the unqualified-column path, so a
    budget-dropped-but-real column is not falsely flagged."""
    slice_text = json.dumps({
        "tables": ["normalized.holding", "normalized.holding_payout"],
        "columns": [{"table_id": "normalized.holding", "name": "holding_id"}],
        "joins": [],
    })
    sql = "SELECT hp.holding_id FROM normalized.holding_payout hp"
    missing = check_grounding(sql=sql, slice_text=slice_text, dialect="athena")
    assert missing == []


def test_feedback_names_available_columns_for_flagged_table():
    """build_grounding_feedback enriches a flagged column with the real columns
    of its table + the slice tables, so regeneration can self-correct in-loop
    rather than re-guessing a sibling column."""
    slice_text = json.dumps({
        "tables": ["normalized.holding", "normalized.annuity_detail"],
        "columns": [
            {"table_id": "normalized.holding", "name": "holding_id"},
            {"table_id": "normalized.holding", "name": "market_value"},
            {"table_id": "normalized.annuity_detail", "name": "holding_id"},
            {"table_id": "normalized.annuity_detail", "name": "premium_mode"},
        ],
        "joins": [],
    })
    fb = build_grounding_feedback(
        missing=["column:holding.payout_frequency"], slice_text=slice_text,
    )
    # Names the real columns of the flagged table...
    assert "market_value" in fb and "holding_id" in fb
    # ...and surfaces the other slice table as an alternative.
    assert "annuity_detail" in fb
    # ...and identifies the offending column.
    assert "payout_frequency" in fb


def test_feedback_empty_for_no_missing():
    """No missing identifiers → empty feedback string."""
    assert build_grounding_feedback(missing=[], slice_text="{}") == ""


def test_feedback_falls_back_on_unparseable_slice():
    """An unparseable slice still yields a non-crashing feedback string naming
    the missing identifiers."""
    fb = build_grounding_feedback(
        missing=["column:foo.bar", "table:baz"], slice_text="not json",
    )
    assert "bar" in fb and "baz" in fb
