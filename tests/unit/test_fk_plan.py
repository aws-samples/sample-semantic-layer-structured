from agents.shared.fk_plan import build_fk_plan, fk_closure

FRAGMENTS = {
    "tables": [
        {"table_name": "coverage", "fk_hints": "holding_id→holding, party_id→party"},
        {"table_name": "holding", "fk_hints": ""},
        {"table_name": "party", "fk_hints": ""},
    ]
}


def test_build_fk_plan_parses_hints():
    plan = build_fk_plan(FRAGMENTS)
    assert plan["coverage"] == [
        {"fk_column": "holding_id", "target_table": "holding"},
        {"fk_column": "party_id", "target_table": "party"},
    ]
    assert "holding" not in plan  # no hints → absent


def test_fk_closure_pulls_in_inbound_neighbors():
    # holding changed → coverage (points at holding) must rebuild too.
    plan = build_fk_plan(FRAGMENTS)
    closure = fk_closure({"holding"}, plan)
    assert closure == {"holding", "coverage"}


def test_fk_closure_includes_outbound_targets():
    # coverage changed → its targets (holding, party) may need edge refresh.
    plan = build_fk_plan(FRAGMENTS)
    closure = fk_closure({"coverage"}, plan)
    assert closure == {"coverage", "holding", "party"}
