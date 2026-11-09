"""Convert mined-rule artefacts into DRAFT Rule models.

The doc-pipeline's ruleminer stage cannot write RDF (its asset has no ``agents/``
tree and no Neptune access), so it drops plain dicts in S3. This module is the
single place those dicts become typed ``Rule`` objects, which then go through
``RuleService.create`` like any hand-authored rule.

Four invariants are enforced HERE rather than trusted to the producer, because
the artefact is a plain JSON file an admin can edit before importing:

* no threshold is imported as approved — approval is a human action;
* no conclusion is imported as attested — ``confirmedCauseOf`` needs a named
  human, and the miner may not attest to its own causal claim;
* no ``compiled_sparql`` is ever imported — compilation happens at publish;
* an out-of-vocabulary ``ruleType`` or ``epistemicStatus`` is downgraded to the
  weakest legal value rather than passed through.

Structural defects (a threshold with no ``sourceSpan``, a rule with no
conditions) are NOT repaired: the models raise, the route returns 422, and the
admin learns the artefact is unusable. A best-effort partial import would hide a
miner regression behind a plausible-looking DRAFT.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from agents.shared.rule_models import (
    Conclusion,
    Condition,
    Provenance,
    Rule,
    RuleScope,
    Threshold,
)
from agents.shared.rule_vocab import DEFAULT_EPISTEMIC_STATUS, EPISTEMIC_STATUSES

# The miner's ruleType vocabulary can be broader than the model's Literal set
# (an artefact is hand-editable, and a future miner prompt may add words), so map
# explicitly. Anything unrecognised becomes 'advisory' — the same weakest value
# the miner's own _DEFAULT_RULE_TYPE uses, so a rule's type cannot change as it
# crosses the artefact boundary. See tests/unit/test_rule_import.py, which pins
# the two together.
_DEFAULT_RULE_TYPE = 'advisory'
_RULE_TYPE_MAP: Dict[str, str] = {
    'diagnostic': 'diagnostic',
    'risk': 'risk',
    'compliance': 'compliance',
    'advisory': 'advisory',
    # Loose synonyms a document might yield. 'eligibility' is an obligation
    # ("must hold X to qualify"), so it reads as compliance; 'classification'
    # assigns a category, which is what a diagnostic rule does.
    'eligibility': 'compliance',
    'classification': 'diagnostic',
}


def _rule_id(*, document_id: str, index: int, name: str) -> str:
    """Build a stable id so re-importing an artefact does not duplicate rules.

    Keyed on the document too: two documents stating the same rule are two
    DRAFTs to review, not one silently overwriting the other.

    :param document_id: Source document id.
    :param index: Position of the rule within the artefact.
    :param name: The mined rule name.
    :returns: A deterministic ``RULE-<hash>`` identifier.
    """
    digest = hashlib.sha256(
        f'{document_id}|{index}|{name}'.encode('utf-8')
    ).hexdigest()[:12]
    return f'RULE-{digest}'


def _condition_from(*, mined: Dict[str, Any]) -> Condition:
    """Build one Condition, passing exactly one comparand.

    ``Condition`` enforces EXACTLY ONE of ``threshold_id`` / ``value``, so the
    absent one must be OMITTED rather than passed as an empty string. The miner's
    parser already guarantees exactly one per surviving condition; a condition
    carrying neither is corrupt input and the model raises.

    :param mined: One mined condition dict.
    :returns: The validated condition.
    :raises ValueError: If the clause has neither or both comparands, or an
        operator outside the allowed set.
    """
    kwargs: Dict[str, Any] = {
        'binding': str(mined.get('binding', '')),
        'operator': str(mined.get('operator', '')),
    }
    if mined.get('thresholdId'):
        kwargs['threshold_id'] = str(mined['thresholdId'])
    elif mined.get('value') is not None:
        kwargs['value'] = mined['value']
    return Condition(**kwargs)


def mined_rules_to_drafts(
    *, mined: Dict[str, Any], ontology_id: str
) -> List[Rule]:
    """Convert one mined-rules artefact into DRAFT Rule models.

    :param mined: The artefact body (``documentId`` + ``rules``).
    :param ontology_id: The layer the rules belong to.
    :returns: DRAFT rules — unapproved, unattested and uncompiled.
    :raises ValueError: If a threshold arrives without a verbatim source span,
        or a rule is otherwise structurally invalid. Failing loudly is correct:
        a malformed artefact means the miner regressed, and importing a
        best-effort subset would hide that.
    """
    document_id = str(mined.get('documentId', ''))
    out: List[Rule] = []
    for index, raw in enumerate(mined.get('rules', [])):
        scope_raw = raw.get('scope', {})
        conclusion_raw = raw.get('conclusion', {})
        status = str(conclusion_raw.get('epistemicStatus', ''))
        page_raw = raw.get('page')
        out.append(
            Rule(
                rule_id=_rule_id(
                    document_id=document_id,
                    index=index,
                    name=str(raw.get('name', '')),
                ),
                # Rule's field is `namespace` and it holds the ontologyId; there
                # is no `ontology_id` field, and passing one would raise.
                namespace=ontology_id,
                name=str(raw.get('name', '')),
                description=str(raw.get('description', '')),
                rule_type=_RULE_TYPE_MAP.get(
                    str(raw.get('ruleType', '')), _DEFAULT_RULE_TYPE
                ),
                # Mined rules always carry a scope, so they are context-bound by
                # construction — there is no general→variant fan-out to choose.
                level='contextualised',
                lifecycle='DRAFT',
                scope=RuleScope(
                    subject_class=str(scope_raw.get('subjectClass', '')),
                    process_stage=str(scope_raw.get('processStage', '')) or None,
                ),
                conditions=[
                    _condition_from(mined=c) for c in raw.get('conditions', [])
                ],
                thresholds=[
                    Threshold(
                        threshold_id=str(t.get('thresholdId', '')),
                        value=float(t['value']),
                        unit=str(t.get('unit', '')),
                        # min_length=1 on the model, so an evidence-free bound
                        # cannot be constructed at all — the second enforcement
                        # point for "never invent a threshold".
                        source_span=str(t.get('sourceSpan', '')),
                        approved_by=None,  # never from the artefact
                    )
                    for t in raw.get('thresholds', [])
                ],
                conclusion=Conclusion(
                    outcome=str(conclusion_raw.get('outcome', '')),
                    # The miner already clamped this, but an artefact can be
                    # hand-edited before import; an unknown status must not
                    # become a causal claim by accident.
                    epistemic_status=(
                        status
                        if status in EPISTEMIC_STATUSES
                        else DEFAULT_EPISTEMIC_STATUS
                    ),
                    attested_by=None,  # never from the artefact
                    attestation_note=None,
                ),
                missing_inputs=[str(m) for m in raw.get('missingInputs', [])],
                compiled_sparql=None,  # never imported
                provenance=Provenance(
                    document_id=document_id,
                    source_sentence=str(raw.get('sourceSentence', '')),
                    model_id=str(raw.get('modelId', '')),
                    # The miner does not score itself, and a self-reported LLM
                    # confidence would be noise shown to a reviewer as if it had
                    # been measured. 0.0 records "unscored"; nothing filters on
                    # this field — the human review gate is what governs.
                    confidence=float(raw.get('confidence', 0.0)),
                    # Genuinely unknown: the chunker's offsets would be where to
                    # derive it later. None, not a plausible-looking 1.
                    page=int(page_raw) if page_raw is not None else None,
                ),
            )
        )
    return out
