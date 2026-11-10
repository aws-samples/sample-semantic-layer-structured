"""Mined-artefact → DRAFT Rule conversion.

The ruleminer stage cannot write RDF, so it drops plain dicts in S3 and this
module is the only place they become typed rules. Every test here guards a point
where a permissive conversion would launder an unreviewed number into something
that looks governed.
"""
import os
import sys

import pytest

# The REST-API package root is not on sys.path by default (only the repo root is,
# via pyproject's `pythonpath`), so `services.*` needs the same shim every other
# rest-api unit test uses.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from typing import Any, Dict  # noqa: E402

from agents.shared.rule_vocab import DEFAULT_EPISTEMIC_STATUS  # noqa: E402
from services.rule_import import mined_rules_to_drafts  # noqa: E402


def _mined(**over: Any) -> Dict[str, Any]:
    """Build one well-formed mined rule as the miner would emit it.

    :param over: Fields to override on the rule.
    :returns: The mined-rule dict (camelCase wire keys).
    """
    rule: Dict[str, Any] = {
        'name': 'lapse risk after non-payment',
        'description': 'a holding lapses after 90 days of non-payment',
        'ruleType': 'diagnostic',
        'scope': {
            'subjectClass': 'https://ex.org/vkg#Holding',
            'processStage': 'renewal',
        },
        'conditions': [
            {
                'binding': 'https://ex.org/vkg#daysSinceLastPayment',
                'operator': '>',
                'thresholdId': 'THR-1',
            },
        ],
        'thresholds': [
            {
                'thresholdId': 'THR-1',
                'value': 90.0,
                'unit': 'days',
                'sourceSpan': 'lapses after 90 days',
            },
        ],
        'conclusion': {
            'outcome': 'https://ex.org/vkg#LapseRisk',
            'epistemicStatus': 'candidateContributorTo',
        },
        'missingInputs': [],
        'sourceSentence': 'A holding lapses after 90 days of non-payment.',
    }
    rule.update(over)
    return rule


def test_a_mined_rule_becomes_a_draft() -> None:
    rules = mined_rules_to_drafts(
        mined={'documentId': 'doc-1', 'rules': [_mined()]},
        ontology_id='ont-1',
    )
    assert len(rules) == 1
    assert rules[0].lifecycle == 'DRAFT'
    # Rule's field is `namespace`; it holds the ontologyId.
    assert rules[0].namespace == 'ont-1'
    assert rules[0].level == 'contextualised'
    assert rules[0].rule_type == 'diagnostic'
    assert rules[0].scope.process_stage == 'renewal'


def test_an_unknown_rule_type_maps_to_a_legal_literal() -> None:
    """`eligibility` is not in Rule.rule_type's Literal set — passing it through
    would raise a ValidationError on every artefact labelled loosely, which the
    admin would see as "import found 0 rules"."""
    rule = mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': [_mined(ruleType='eligibility')]},
        ontology_id='ont-1',
    )[0]
    assert rule.rule_type == 'compliance'


def test_an_unrecognised_rule_type_falls_back_to_the_weakest_reading() -> None:
    """Agreement test with the miner's own fallback. A type nobody recognises
    must become the weakest classification, never a compliance obligation —
    and both halves of the wire must pick the SAME weakest one, or a rule's
    type silently changes as it crosses the artefact boundary."""
    if 'handler' in sys.modules:
        del sys.modules['handler']
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(__file__), '..', '..',
            'lambda', 'doc-pipeline', 'ruleminer',
        ),
    )
    try:
        import handler  # noqa: WPS433 — bundled Lambda module, see docstring
        miner_default = handler._DEFAULT_RULE_TYPE
    finally:
        del sys.modules['handler']
    rule = mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': [_mined(ruleType='wat')]},
        ontology_id='ont-1',
    )[0]
    assert rule.rule_type == miner_default


def test_an_unknown_epistemic_status_falls_back_to_the_default() -> None:
    """A hand-edited artefact must not smuggle in an illegal status."""
    rule = mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': [_mined(
            conclusion={'outcome': 'https://ex.org/vkg#LapseRisk',
                        'epistemicStatus': 'definitelyProves'})]},
        ontology_id='ont-1',
    )[0]
    assert rule.conclusion.epistemic_status == DEFAULT_EPISTEMIC_STATUS


def test_an_imported_rule_is_never_pre_attested() -> None:
    """`confirmedCauseOf` needs a named human in `attested_by`; an artefact
    claiming one would let the miner attest to its own causal claim."""
    rule = mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': [_mined(
            conclusion={'outcome': 'https://ex.org/vkg#LapseRisk',
                        'epistemicStatus': 'confirmedCauseOf',
                        'attestedBy': 'the model itself'})]},
        ontology_id='ont-1',
    )[0]
    assert rule.conclusion.attested_by is None


def test_a_literal_condition_imports_under_value() -> None:
    """Pairs with the miner's wire-contract test — the two must agree."""
    rule = mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': [_mined(
            conditions=[{'binding': 'https://ex.org/vkg#status',
                         'operator': '=', 'value': 'Active'}],
            thresholds=[])]},
        ontology_id='ont-1',
    )[0]
    assert rule.conditions[0].value == 'Active'
    assert rule.conditions[0].threshold_id is None


def test_imported_thresholds_are_never_pre_approved() -> None:
    """The human-owned gate: import cannot approve anything."""
    rule = mined_rules_to_drafts(
        mined={'documentId': 'doc-1',
               'rules': [_mined(thresholds=[
                   {'thresholdId': 'THR-1', 'value': 90.0, 'unit': 'days',
                    'sourceSpan': 'after 90 days', 'approvedBy': 'attacker'},
               ])]},
        ontology_id='ont-1',
    )[0]
    assert rule.thresholds[0].approved_by is None
    assert rule.thresholds[0].is_approved() is False


def test_provenance_carries_the_document_and_source_sentence() -> None:
    rule = mined_rules_to_drafts(
        mined={'documentId': 'doc-42', 'rules': [_mined()]},
        ontology_id='ont-1',
    )[0]
    assert rule.provenance.document_id == 'doc-42'
    assert '90 days' in rule.provenance.source_sentence


def test_a_mined_rule_imports_as_unscored_rather_than_confident() -> None:
    """`Provenance.confidence` is required by the model but the miner does not
    score itself. 0.0 records "unscored"; anything higher would be a number
    nobody measured, shown to a reviewer as if it had been."""
    rule = mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': [_mined()]}, ontology_id='ont-1',
    )[0]
    assert rule.provenance.confidence == 0.0
    # The page is genuinely unknown — None, not a plausible-looking 1.
    assert rule.provenance.page is None


def test_missing_inputs_survive_the_round_trip() -> None:
    """This is the flag that later routes the rule to advise, not evaluate."""
    rule = mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': [_mined(
            thresholds=[],
            missingInputs=['https://ex.org/vkg#daysSinceLastPayment'])]},
        ontology_id='ont-1',
    )[0]
    assert rule.missing_inputs == ['https://ex.org/vkg#daysSinceLastPayment']
    assert rule.is_evaluable() is False


def test_a_threshold_without_a_source_span_raises() -> None:
    """Fail loudly — do not quietly drop evidence-free numbers into a DRAFT."""
    with pytest.raises(ValueError):
        mined_rules_to_drafts(
            mined={'documentId': 'd', 'rules': [_mined(thresholds=[
                {'thresholdId': 'THR-1', 'value': 90.0, 'unit': 'days',
                 'sourceSpan': ''},
            ])]},
            ontology_id='ont-1',
        )


def test_a_rule_with_no_conditions_raises() -> None:
    """The miner drops these; an artefact still carrying one is corrupt input,
    and importing a conditionless "rule" would produce a claim with no trigger."""
    with pytest.raises(ValueError):
        mined_rules_to_drafts(
            mined={'documentId': 'd', 'rules': [_mined(conditions=[])]},
            ontology_id='ont-1',
        )


def test_no_compiled_sparql_is_ever_imported() -> None:
    rule = mined_rules_to_drafts(
        mined={'documentId': 'd',
               'rules': [_mined(compiledSparql='SELECT * {}')]},
        ontology_id='ont-1',
    )[0]
    assert not rule.compiled_sparql


def test_an_empty_artefact_imports_nothing() -> None:
    assert mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': []}, ontology_id='ont-1') == []


def test_rule_ids_are_unique_and_deterministic_per_document() -> None:
    """Re-importing the same artefact must not create duplicate rules."""
    first = mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': [_mined(), _mined(name='other')]},
        ontology_id='ont-1')
    second = mined_rules_to_drafts(
        mined={'documentId': 'd', 'rules': [_mined(), _mined(name='other')]},
        ontology_id='ont-1')
    assert [r.rule_id for r in first] == [r.rule_id for r in second]
    assert len({r.rule_id for r in first}) == 2


def test_rule_ids_differ_across_documents() -> None:
    """Two documents stating the same rule are two DRAFTs to review, not one
    silently overwriting the other."""
    a = mined_rules_to_drafts(
        mined={'documentId': 'doc-a', 'rules': [_mined()]},
        ontology_id='ont-1')[0]
    b = mined_rules_to_drafts(
        mined={'documentId': 'doc-b', 'rules': [_mined()]},
        ontology_id='ont-1')[0]
    assert a.rule_id != b.rule_id
