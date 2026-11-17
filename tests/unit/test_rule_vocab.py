"""Vocabulary constants for the vkgr: rule namespace."""
from agents.shared import rule_vocab as v


def test_namespace_is_stable() -> None:
    """The IRI prefix is a contract — a change orphans every stored rule."""
    assert str(v.VKGR) == "https://semantic-layer.aws/virtual-kg/rules#"


def test_epistemic_statuses_are_closed_and_ordered_weakest_first() -> None:
    """Order matters: the parser clamps DOWN this list, never up."""
    assert v.EPISTEMIC_STATUSES == (
        "associatedWith",
        "requiresFurtherEvidence",
        "candidateContributorTo",
        "supportsHypothesis",
        "indicatesRiskOf",
        "confirmedCauseOf",
    )


def test_default_epistemic_status_is_not_causal() -> None:
    """An unclamped extraction must never default to a causal claim."""
    assert v.DEFAULT_EPISTEMIC_STATUS == "candidateContributorTo"
    assert v.STRONGEST_EPISTEMIC_STATUS == "confirmedCauseOf"


def test_rules_graph_suffix_matches_design() -> None:
    assert v.RULES_GRAPH_SUFFIX == "/rules"


def test_allowed_operators_are_scalar_comparisons_only() -> None:
    """Declared HERE and nowhere else. The model validates against this set
    and the compiler renders from it; two copies would let a rule validate
    and then refuse to compile at publish."""
    assert v.ALLOWED_OPERATORS == frozenset(
        {"=", "!=", ">", ">=", "<", "<="}
    )
