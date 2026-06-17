"""Unit tests for the Phase 3b slice-level disambiguation guard."""
from agents.metadata_query_agent.tier2.slice_disambiguation import (
    detect_unsupported_relationship,
    find_slice_ambiguities,
)


def _slice(columns, joins=None, tables=None):
    """Build a slice dict from (table_id, column) pairs."""
    cols = [{"table_id": t, "name": c} for t, c in columns]
    tabs = tables or sorted({t for t, _ in columns})
    return {"tables": tabs, "columns": cols, "joins": joins or []}


def test_column_collision_across_two_tables_is_ambiguous():
    slice_obj = _slice([("db.orders", "amount"), ("db.payments", "amount")])
    res = find_slice_ambiguities(question="total amount", slice_obj=slice_obj)
    assert res["ambiguous"] is True
    assert res["items"][0]["term"] == "amount"
    assert len(res["items"][0]["matches"]) == 2


def test_single_table_column_not_ambiguous():
    slice_obj = _slice([("db.orders", "amount")])
    res = find_slice_ambiguities(question="total amount", slice_obj=slice_obj)
    assert res["ambiguous"] is False


def test_join_graph_resolves_collision_heuristically():
    # 'amount' is on both tables, but only db.orders is connected in the join
    # graph → resolve to db.orders without bothering the user.
    slice_obj = _slice(
        [("db.orders", "amount"), ("db.payments", "amount"),
         ("db.orders", "id"), ("db.customers", "id")],
        joins=[{"from": "db.orders", "to": "db.customers",
                "from_col": "id", "to_col": "id"}],
    )
    res = find_slice_ambiguities(question="total amount", slice_obj=slice_obj)
    assert res["ambiguous"] is False
    assert res["resolved"].get("amount") == "db.orders"


def test_two_connected_owners_stay_ambiguous():
    # both owners connected → cannot pick heuristically → clarify.
    slice_obj = _slice(
        [("db.orders", "amount"), ("db.payments", "amount")],
        joins=[{"from": "db.orders", "to": "db.payments",
                "from_col": "x", "to_col": "y"}],
    )
    res = find_slice_ambiguities(question="total amount", slice_obj=slice_obj)
    assert res["ambiguous"] is True


# --- Phase 3b unsupported-relationship guard -------------------------------

def _col(table, name, desc=""):
    """A slice column dict with an optional description (carries enum values)."""
    return {"table_id": table, "name": name, "description": desc}


def _relation_slice_real_schema():
    """A slice mirroring the REAL curated `relation` schema (party-to-party only).

    `relation` carries interpersonal roles (Parent/Beneficiary/Trustee/Spouse/
    Sibling) — no Insured/Owner/Policyholder — and `coverage` carries the insured
    party only. There is NO policyholder/owner role column anywhere.
    """
    cols = [
        _col("normalized.relation", "party_id_1",
             "Originating party in this relationship. FK to party."),
        _col("normalized.relation", "party_id_2",
             "Target/related party in this relationship. FK to party."),
        _col("normalized.relation", "relationship_role",
             "Role classification within the relationship. Values: Primary, "
             "Secondary."),
        _col("normalized.relation", "relation_type",
             "Category of relationship. Values: Trustee, Parent, Spouse, Sibling, "
             "Beneficiary."),
        _col("normalized.coverage", "party_id", "Insured party on this coverage."),
        _col("normalized.coverage", "policy_id", "Policy identifier."),
        _col("normalized.party", "party_id", "PK."),
        _col("normalized.party", "full_name", "Display name."),
    ]
    return {"tables": ["normalized.relation", "normalized.coverage",
                       "normalized.party"], "columns": cols, "joins": []}


def test_insured_equals_policyholder_unsupported():
    q = "Show me policies where the insured party is also the policyholder."
    reason = detect_unsupported_relationship(
        question=q, slice_obj=_relation_slice_real_schema())
    assert reason  # non-empty user-facing string
    assert "policyholder" in reason.lower() or "owner" in reason.lower()


def test_beneficiary_question_not_pre_empted():
    # Single role word that IS a real enumerated value → do not fast-fail.
    q = "List the beneficiaries for each policy."
    reason = detect_unsupported_relationship(
        question=q, slice_obj=_relation_slice_real_schema())
    assert reason is None


def test_non_role_question_not_pre_empted():
    q = "What is the total face amount per product?"
    reason = detect_unsupported_relationship(
        question=q, slice_obj=_relation_slice_real_schema())
    assert reason is None


def test_owner_role_present_not_pre_empted():
    # A model that DOES carry an owner/policyholder role → guard must not fire.
    cols = [
        _col("normalized.policy_party_role", "role_code",
             "Party role on the policy. Values: Owner, Insured, Payor."),
        _col("normalized.policy_party_role", "party_id", "FK to party."),
        _col("normalized.policy_party_role", "policy_id", "FK to policy."),
    ]
    slice_obj = {"tables": ["normalized.policy_party_role"], "columns": cols,
                 "joins": []}
    q = "Show me policies where the insured party is also the policyholder."
    reason = detect_unsupported_relationship(question=q, slice_obj=slice_obj)
    assert reason is None
