"""Unit tests for the Phase 3b slice-level disambiguation guard."""
import inspect

from agents.metadata_query_agent.tier2 import slice_disambiguation
from agents.metadata_query_agent.tier2.slice_disambiguation import (
    _representable_role_groups,
    _role_vocabulary,
    _unsupported_reason,
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


# --- Phase 3b unsupported-relationship guard (de-layered vocabulary) -------
#
# The role vocabulary is no longer hard-coded — it is parsed at runtime from the
# `Values: … (synonyms: …)` / `Role values include: …` enumeration the metadata
# agent authors into `columns[].description` (design 2026-06-26-delayer-slice-
# disambiguation-role-vocab). These tests pin that de-layered contract.

def _col(table, name, desc=""):
    """A slice column dict with an optional description (carries enum values)."""
    return {"table_id": table, "name": name, "description": desc}


def _relation_slice_real_schema():
    """A slice mirroring the REAL curated `relation` schema (party-to-party only).

    `relation` carries interpersonal roles (Parent/Beneficiary/Trustee/Spouse/
    Sibling) — no Insured/Owner/Policyholder — and `coverage` names the insured
    party in prose only. There is NO policy party-role enumeration declaring
    Owner/Policyholder, so the derived vocabulary never contains an `owner` group.
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


def _b1_life_participant_slice():
    """The B1-enriched `life_participant` slice (parent doc §3.5).

    Its role column declares `Owner (synonyms: Policyholder), Insured,
    Beneficiary` — the policy party-role enumeration that makes gt-00
    ('insured is also the policyholder') ANSWERABLE via a self-join, so the guard
    must NOT fire for it.
    """
    cols = [
        _col("normalized.life_participant", "participant_role",
             "Role of the party on the policy. Role values include: Owner "
             "(synonyms: Policyholder), Insured, Beneficiary. Each value is a "
             "distinct policy party-role."),
        _col("normalized.life_participant", "holding_id",
             "FK to holding. Self-join key (pair with party_id)."),
        _col("normalized.life_participant", "party_id", "FK to party."),
    ]
    return {"tables": ["normalized.life_participant"], "columns": cols, "joins": []}


# --- vocabulary derivation -------------------------------------------------

def test_role_vocabulary_parses_values_with_synonyms():
    word_to_group, group_tokens = _role_vocabulary(_b1_life_participant_slice())
    # Canonical groups come from the enumeration labels.
    assert set(group_tokens) == {"owner", "insured", "beneficiary"}
    # Synonym maps to the canonical group; plural inflection resolves too.
    assert word_to_group["policyholder"] == "owner"
    assert word_to_group["policyholders"] == "owner"
    assert word_to_group["owner"] == "owner"
    assert word_to_group["insured"] == "insured"
    # The synonym is also evidence the slice represents the group.
    assert "policyholder" in group_tokens["owner"]


def test_role_vocabulary_empty_when_no_enumeration():
    slice_obj = {
        "tables": ["t"],
        "columns": [_col("t", "x", "just prose, no enumerated values")],
        "joins": [],
    }
    word_to_group, group_tokens = _role_vocabulary(slice_obj)
    assert word_to_group == {}
    assert group_tokens == {}


def test_representable_groups_from_derived_tokens():
    slice_obj = _b1_life_participant_slice()
    _, group_tokens = _role_vocabulary(slice_obj)
    groups = _representable_role_groups(slice_obj, group_tokens)
    # The enumeration text carries Owner/Insured/Beneficiary, so all three are
    # representable from column metadata alone.
    assert groups == {"owner", "insured", "beneficiary"}


# --- guard behaviour -------------------------------------------------------

def test_gt00_not_pre_empted_when_b1_enumeration_present():
    # B1 makes gt-00 answerable via the life_participant self-join: the role
    # enumeration declares BOTH Owner/Policyholder and Insured, so every
    # referenced role is representable → the guard must NOT fast-fail.
    q = "Show me policies where the insured party is also the policyholder."
    reason = detect_unsupported_relationship(
        question=q, slice_obj=_b1_life_participant_slice())
    assert reason is None


def test_no_op_when_slice_declares_no_role_enumeration():
    # The real `relation` schema declares no policy party-role enumeration (only
    # interpersonal Primary/Secondary roles), so the derived vocabulary has no
    # owner/insured comparison groups → the guard is a no-op (design §4c): absent
    # supporting metadata we do not invent a domain-specific fast-fail.
    q = "Show me policies where the insured party is also the policyholder."
    reason = detect_unsupported_relationship(
        question=q, slice_obj=_relation_slice_real_schema())
    assert reason is None


def test_beneficiary_question_not_pre_empted():
    # Single role word → never a same-entity role comparison → no fast-fail.
    q = "List the beneficiaries for each policy."
    reason = detect_unsupported_relationship(
        question=q, slice_obj=_b1_life_participant_slice())
    assert reason is None


def test_non_role_question_not_pre_empted():
    q = "What is the total face amount per product?"
    reason = detect_unsupported_relationship(
        question=q, slice_obj=_b1_life_participant_slice())
    assert reason is None


def test_owner_role_present_not_pre_empted():
    # A model that DOES carry an owner/policyholder enumeration → guard must not
    # fire (both referenced roles are representable).
    cols = [
        _col("normalized.policy_party_role", "role_code",
             "Party role on the policy. Values: Owner (synonyms: Policyholder), "
             "Insured, Payor."),
        _col("normalized.policy_party_role", "party_id", "FK to party."),
        _col("normalized.policy_party_role", "policy_id", "FK to policy."),
    ]
    slice_obj = {"tables": ["normalized.policy_party_role"], "columns": cols,
                 "joins": []}
    q = "Show me policies where the insured party is also the policyholder."
    reason = detect_unsupported_relationship(question=q, slice_obj=slice_obj)
    assert reason is None


def test_referenced_role_unreferenceable_when_not_in_enumeration():
    # De-layering consequence: a role word the question uses but which NO column
    # enumeration declares is simply NOT in the derived vocabulary, so it cannot
    # be 'referenced'. Here only Owner/Policyholder is enumerated; 'insured' is
    # absent from every description → the question references a single group →
    # the guard does not fire. (This is WHY the missing-role branch is unreachable
    # for a slice whose only role evidence is its enumeration prose — see the
    # detect_unsupported_relationship docstring.)
    cols = [
        _col("normalized.policy_owner", "owner_role",
             "Owner role on the policy. Values: Owner (synonyms: Policyholder)."),
    ]
    slice_obj = {"tables": ["normalized.policy_owner"], "columns": cols,
                 "joins": []}
    word_to_group, _ = _role_vocabulary(slice_obj)
    assert "insured" not in word_to_group
    q = "policies where the insured party is also the policyholder"
    assert detect_unsupported_relationship(question=q, slice_obj=slice_obj) is None


def test_unsupported_reason_is_generated_not_literal():
    # The degrade reason is built from the derived role labels + scanned tables —
    # no insurance prose literal. Tests the pure reason builder directly (the
    # branch that calls it is an unreachable-via-API defensive path; see docstring).
    reason = _unsupported_reason(
        missing_groups=["insured"],
        other_groups=["owner"],
        tables=["normalized.policy_owner"],
    )
    assert "insured" in reason
    assert "owner" in reason
    assert "normalized.policy_owner" in reason  # names the scanned schema
    # No hard-coded insurance prose from the pre-de-layering version.
    assert "current schema." not in reason


def test_no_insurance_literals_in_source():
    # Success criterion: no insurance-specific role tokens or prose literals
    # remain in the module — the vocabulary and reason are both runtime-derived.
    src = inspect.getsource(slice_disambiguation).lower()
    # The hard-coded role maps and the literal degrade prose must be gone. Check
    # the actual runtime STRING LITERALS, not docstring references to them.
    assert "_role_word_to_group" not in src
    assert "_role_group_tokens" not in src
    # The pre-de-layering degrade literal began "this data model records party
    # relationships, but it has no policyholder / owner role on a policy" — its
    # distinctive fragment must not appear anywhere in the module.
    assert "records party relationships" not in src
    assert "no policyholder / owner role" not in src
