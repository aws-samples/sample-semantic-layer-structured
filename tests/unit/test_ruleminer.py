"""ruleminer parser + fail-soft extraction.

Imports the Lambda the same way ``test_ner_lambda.py`` does — ``sys.path``
insertion plus a plain ``import handler`` — because the doc-pipeline assets are
bundled standalone and are not packages. The autouse fixture drops ``handler``
from ``sys.modules`` before every test so a sibling stage's handler (``ner`` has
one too) can never be the module under test.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import pytest


@pytest.fixture(autouse=True)
def reset_module():
    """Drop any previously-imported ``handler`` so the path insert below wins."""
    if 'handler' in sys.modules:
        del sys.modules['handler']
    yield
    if 'handler' in sys.modules:
        del sys.modules['handler']


def _import_handler():
    """Import the ruleminer stage's bundled handler module.

    :returns: The imported ``handler`` module.
    """
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(__file__),
            '..',
            '..',
            'lambda',
            'doc-pipeline',
            'ruleminer',
        ),
    )
    if 'handler' in sys.modules:
        del sys.modules['handler']
    import handler  # noqa: WPS433 — deliberate late import, see module docstring
    return handler


def _payload(**over) -> str:
    """Build a model response containing one well-formed mined rule.

    :param over: Fields to override on the rule.
    :returns: The JSON text a model would emit.
    """
    rule: Dict[str, Any] = {
        'name': 'lapse risk after non-payment',
        'description': 'a holding lapses after 90 days of non-payment',
        'ruleType': 'diagnostic',
        'scope': {'subjectClass': 'https://ex.org/vkg#Holding'},
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
                'value': 90,
                'unit': 'days',
                'sourceSpan': 'lapses after 90 days of non-payment',
            },
        ],
        'conclusion': {
            'outcome': 'https://ex.org/vkg#LapseRisk',
            'epistemicStatus': 'candidateContributorTo',
        },
        'sourceSentence': 'A holding lapses after 90 days of non-payment.',
    }
    rule.update(over)
    return json.dumps({'rules': [rule]})


class _FakeBody:
    """Minimal stand-in for a Bedrock streaming body."""

    def __init__(self, text: str) -> None:
        """:param text: The response payload to hand back from ``read``."""
        self._text = text

    def read(self) -> bytes:
        """:returns: The payload as UTF-8 bytes."""
        return self._text.encode('utf-8')


# ── Parser ──────────────────────────────────────────────────────────────────


def test_handler_module_has_the_mining_logic_inlined() -> None:
    """The agents/ tree is not bundled with the asset, so every symbol the
    stage needs must live in handler itself."""
    handler = _import_handler()
    for name in (
        'RULE_MINER_SYSTEM_PROMPT',
        'clamp_epistemic_status',
        'parse_rule_output',
        'mine_rules',
        'mine_chunks',
        'handler',
    ):
        assert hasattr(handler, name), name


def test_a_wellformed_rule_parses() -> None:
    out = _import_handler().parse_rule_output(_payload())
    assert len(out) == 1
    assert out[0]['thresholds'][0]['value'] == 90


def test_fenced_json_is_unwrapped() -> None:
    """Same ```-fence tolerance as parse_ner_output."""
    parse = _import_handler().parse_rule_output
    assert len(parse('```json\n' + _payload() + '\n```')) == 1


def test_unparseable_output_yields_no_rules() -> None:
    assert _import_handler().parse_rule_output('not json at all') == []


def test_a_rule_with_no_conditions_is_dropped() -> None:
    assert _import_handler().parse_rule_output(_payload(conditions=[])) == []


def test_a_rule_with_no_conclusion_outcome_is_dropped() -> None:
    parse = _import_handler().parse_rule_output
    assert parse(_payload(conclusion={'outcome': ''})) == []


def test_a_threshold_without_a_source_span_becomes_a_missing_input() -> None:
    """The invented-threshold guard: no textual evidence, no number."""
    out = _import_handler().parse_rule_output(_payload(thresholds=[
        {'thresholdId': 'THR-1', 'value': 90, 'unit': 'days', 'sourceSpan': ''},
    ]))
    assert out[0]['thresholds'] == []
    assert 'https://ex.org/vkg#daysSinceLastPayment' in out[0]['missingInputs']


def test_a_condition_referencing_an_unknown_threshold_becomes_a_missing_input() -> None:
    out = _import_handler().parse_rule_output(_payload(thresholds=[]))
    assert out[0]['missingInputs'] == [
        'https://ex.org/vkg#daysSinceLastPayment'
    ]


def test_a_non_numeric_threshold_value_is_dropped() -> None:
    out = _import_handler().parse_rule_output(_payload(thresholds=[
        {
            'thresholdId': 'THR-1',
            'value': 'about ninety',
            'unit': 'days',
            'sourceSpan': 'after about ninety days',
        },
    ]))
    assert out[0]['thresholds'] == []


def test_an_unknown_operator_drops_the_condition_and_then_the_rule() -> None:
    out = _import_handler().parse_rule_output(_payload(conditions=[
        {'binding': 'https://ex.org/vkg#x', 'operator': 'roughly exceeds'},
    ]))
    assert out == []


def test_a_literal_comparand_is_emitted_under_the_key_value() -> None:
    """Wire-contract test. ``rule_import.mined_rules_to_drafts`` maps this
    artefact directly onto ``Condition``, whose field is ``value``. If this key
    ever drifts to ``literal``, every literal condition is silently dropped at
    import and rules quietly lose clauses — with no error anywhere."""
    out = _import_handler().parse_rule_output(_payload(conditions=[
        {
            'binding': 'https://ex.org/vkg#status',
            'operator': '=',
            'value': 'Active',
        },
    ]))
    assert out[0]['conditions'][0]['value'] == 'Active'
    assert 'literal' not in out[0]['conditions'][0]


def test_a_condition_with_both_comparands_is_dropped() -> None:
    """Condition._exactly_one_comparand would raise at import; drop here."""
    out = _import_handler().parse_rule_output(_payload(conditions=[
        {
            'binding': 'https://ex.org/vkg#x',
            'operator': '=',
            'thresholdId': 'THR-1',
            'value': 'Active',
        },
    ]))
    assert out == []


# ── Epistemic clamp ─────────────────────────────────────────────────────────


def test_confirmed_cause_is_clamped_without_causal_language() -> None:
    clamp = _import_handler().clamp_epistemic_status
    assert clamp(
        status='confirmedCauseOf',
        source_sentence='Non-payment may contribute to lapse.',
    ) == 'candidateContributorTo'


def test_confirmed_cause_survives_explicit_causal_language() -> None:
    clamp = _import_handler().clamp_epistemic_status
    assert clamp(
        status='confirmedCauseOf',
        source_sentence='Non-payment causes the holding to lapse.',
    ) == 'confirmedCauseOf'


def test_an_unknown_status_falls_back_to_the_default() -> None:
    clamp = _import_handler().clamp_epistemic_status
    assert clamp(
        status='definitelyProves', source_sentence='x causes y',
    ) == 'candidateContributorTo'


def test_the_clamp_is_applied_during_parsing_not_just_available() -> None:
    out = _import_handler().parse_rule_output(_payload(
        conclusion={
            'outcome': 'https://ex.org/vkg#LapseRisk',
            'epistemicStatus': 'confirmedCauseOf',
        },
        sourceSentence='Non-payment may contribute to lapse.',
    ))
    assert out[0]['conclusion']['epistemicStatus'] == 'candidateContributorTo'


# ── Import-contract agreement with the shared vocabulary ────────────────────
#
# The asset cannot import agents/shared, so it restates the vocabularies as
# literals. These two tests are the only thing standing between a restatement
# drifting and every mined rule failing pydantic validation at import time —
# which surfaces as "import found 0 rules", not as a vocabulary error.


def test_the_status_vocabulary_matches_the_shared_closed_set() -> None:
    from agents.shared.rule_vocab import (
        DEFAULT_EPISTEMIC_STATUS, EPISTEMIC_STATUSES,
    )

    handler = _import_handler()
    assert set(handler._VALID_STATUSES) == set(EPISTEMIC_STATUSES)
    assert handler._DEFAULT_STATUS == DEFAULT_EPISTEMIC_STATUS


def test_every_emittable_rule_type_is_accepted_by_the_rule_model() -> None:
    from typing import get_args

    from agents.shared.rule_models import Rule

    handler = _import_handler()
    allowed = set(get_args(Rule.model_fields['rule_type'].annotation))
    assert handler._VALID_RULE_TYPES <= allowed
    assert handler._DEFAULT_RULE_TYPE in allowed


def test_an_unknown_rule_type_falls_back_to_an_importable_one() -> None:
    handler = _import_handler()
    out = handler.parse_rule_output(_payload(ruleType='eligibility'))
    assert out[0]['ruleType'] == handler._DEFAULT_RULE_TYPE


# ── Chunk orchestration ─────────────────────────────────────────────────────


def test_a_per_chunk_failure_degrades_and_does_not_stop_the_document() -> None:
    class Boom:
        def invoke_model(self, **_kw):
            raise RuntimeError('throttled')

    chunks = _import_handler().mine_chunks(
        chunks=[{'chunkId': 'c1', 'text': 'some text', 'entities': []}],
        bedrock_runtime=Boom(),
    )
    assert chunks[0]['rules'] == []
    assert 'throttled' in chunks[0]['ruleMinerError']


def test_linked_classes_are_offered_to_the_prompt_as_the_binding_vocabulary() -> None:
    """Placement after `link` is the whole reason this stage exists there."""
    seen: List[str] = []

    class Recorder:
        def invoke_model(self, **kwargs):
            seen.append(kwargs['body'].decode('utf-8'))
            return {'body': _FakeBody(json.dumps(
                {'content': [{'type': 'text', 'text': _payload()}]}))}

    _import_handler().mine_chunks(
        chunks=[{
            'chunkId': 'c1',
            'text': 't',
            'entities': [{
                'text': 'holding',
                'linkedClass': 'https://ex.org/vkg#Holding',
            }],
        }],
        bedrock_runtime=Recorder(),
    )
    assert 'https://ex.org/vkg#Holding' in seen[0]


def test_the_prompt_forbids_inventing_thresholds() -> None:
    """Belt and braces: the parser enforces it, the prompt states it."""
    prompt = _import_handler().RULE_MINER_SYSTEM_PROMPT.lower()
    assert 'never' in prompt
    assert 'invent' in prompt
