"""Rule-mining stage Lambda — conditional-statement extraction over Bedrock.

Step Functions invokes this between the link and index stages. Placement is
forced: the miner needs each entity's ``linkedClass`` (written by the linker) to
bind rule roles to ontology concepts, and it must precede ``index`` so Knowledge
Base ingestion still runs last.

Like every doc-pipeline Lambda this asset is self-contained — there is no
``agents/`` tree on the deployed package — so it produces PLAIN DICTS and writes
them to S3. Persisting them as RDF happens later, through the REST API's
``:import`` route, which owns the single Neptune write path and the single
lifecycle enforcement point.

Per-chunk failures degrade to ``rules=[] + ruleMinerError`` so one bad chunk
never fails a document, exactly as the NER stage does.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)


RULE_MINER_SYSTEM_PROMPT = """\
Extract CONDITIONAL BUSINESS RULES from the supplied text. Output ONLY JSON:

{
  "rules": [
    {
      "name": "<short name>",
      "description": "<one sentence>",
      "ruleType": "diagnostic | risk | compliance | advisory",
      "scope": {"subjectClass": "<ontology class IRI>", "processStage": "<optional>"},
      "conditions": [
        {"binding": "<ontology property IRI>", "operator": "= | != | > | >= | < | <=",
         "thresholdId": "<id of a threshold below>", "value": "<OR a literal value>"}
      ],
      "thresholds": [
        {"thresholdId": "...", "value": <number>, "unit": "<unit>",
         "sourceSpan": "<the EXACT words in the text stating this value>"}
      ],
      "conclusion": {"outcome": "<ontology class IRI>",
                     "epistemicStatus": "candidateContributorTo | indicatesRiskOf | "
                                        "supportsHypothesis | associatedWith | "
                                        "requiresFurtherEvidence | confirmedCauseOf"},
      "sourceSentence": "<the sentence this rule came from, verbatim>"
    }
  ]
}

Rules:
- Use ONLY the ontology IRIs listed in the user message as bindings and classes.
- NEVER invent a threshold. If the text states a condition but not its value,
  omit the threshold entirely and leave the condition's thresholdId dangling —
  that is the correct output, and it is recorded as a missing input.
- sourceSpan MUST quote the text verbatim. A threshold with no sourceSpan is discarded.
- Use "confirmedCauseOf" ONLY where the text states causation outright
  ("causes", "results in", "leads to"). Hedged language ("may", "can",
  "is associated with") means candidateContributorTo.
- Each condition needs exactly one of thresholdId or literal.
- If no conditional rules are present, return {"rules": []}.
"""


# Must be a SUBSET of Rule.rule_type's Literal in agents/shared/rule_models.py:
# a value outside it fails pydantic validation at import, which the admin sees
# as "0 rules imported" rather than as a vocabulary mismatch. Restated here
# because the asset cannot import the shared module; tests/unit/test_ruleminer.py
# asserts the agreement.
_VALID_RULE_TYPES = frozenset({'diagnostic', 'risk', 'compliance', 'advisory'})

# What an unrecognised ruleType collapses to. 'advisory' is the weakest reading:
# a rule whose type the model did not state must not claim to be a compliance
# obligation.
_DEFAULT_RULE_TYPE = 'advisory'

_VALID_OPERATORS = frozenset({'=', '!=', '>', '>=', '<', '<='})

# Mirrors EPISTEMIC_STATUSES in agents/shared/rule_vocab.py (weakest first).
# Same restatement reason and same test guarding it as _VALID_RULE_TYPES.
_VALID_STATUSES = (
    'associatedWith',
    'requiresFurtherEvidence',
    'candidateContributorTo',
    'supportsHypothesis',
    'indicatesRiskOf',
    'confirmedCauseOf',
)

# Mirrors DEFAULT_EPISTEMIC_STATUS — what a hedged or unrecognised claim
# collapses to. NOT the weakest member of the ordering above: a hedged causal
# claim is still a candidate contribution, it is simply not a proven cause.
_DEFAULT_STATUS = 'candidateContributorTo'

# Explicit causal verbs. Deliberately short and literal: the point is that a
# machine may not upgrade to a causal claim on the strength of vague wording.
_CAUSAL_MARKERS = ('causes', 'caused by', 'results in', 'leads to', 'because')


def clamp_epistemic_status(*, status: str, source_sentence: str) -> str:
    """Downgrade an overstated epistemic status; never upgrade one.

    ``confirmedCauseOf`` survives only when the source sentence contains an
    explicit causal marker. Anything unrecognised falls back to the default
    status rather than being passed through.

    :param status: The status the model emitted.
    :param source_sentence: The sentence the rule was extracted from.
    :returns: A status guaranteed to be in the closed set.
    """
    if status not in _VALID_STATUSES:
        return _DEFAULT_STATUS
    if status != 'confirmedCauseOf':
        return status
    lowered = (source_sentence or '').lower()
    if any(marker in lowered for marker in _CAUSAL_MARKERS):
        return status
    return _DEFAULT_STATUS


def _strip_fence(raw: str) -> str:
    """Remove a leading ``` fence from an LLM response, if present.

    :param raw: Raw model text.
    :returns: The text with any code fence removed.
    """
    text = raw.strip()
    if not text.startswith('```'):
        return text
    first_newline = text.find('\n')
    if first_newline != -1:
        text = text[first_newline + 1:]
    if text.endswith('```'):
        text = text[:-3]
    return text.strip()


def _parse_threshold(threshold: Any) -> Optional[Dict[str, Any]]:
    """Validate one threshold. Returns None if it must be discarded.

    A threshold is discarded when it has no id, a non-numeric value, or — the
    important case — no verbatim ``sourceSpan``. Discarding rather than
    defaulting is what makes "never invent a threshold" a parse-time guarantee
    instead of a prompt suggestion.

    :param threshold: The raw threshold object from the model.
    :returns: A normalised threshold dict, or None.
    """
    if not isinstance(threshold, dict):
        return None
    threshold_id = threshold.get('thresholdId')
    source_span = threshold.get('sourceSpan')
    if not isinstance(threshold_id, str) or not threshold_id.strip():
        return None
    if not isinstance(source_span, str) or not source_span.strip():
        return None
    try:
        value = float(threshold['value'])
    except (KeyError, TypeError, ValueError):
        return None
    unit = threshold.get('unit')
    return {
        'thresholdId': threshold_id.strip(),
        'value': value,
        'unit': unit.strip() if isinstance(unit, str) else '',
        'sourceSpan': source_span.strip(),
    }


def _parse_condition(condition: Any) -> Optional[Dict[str, Any]]:
    """Validate one condition. Returns None if it must be discarded.

    :param condition: The raw condition object from the model.
    :returns: A normalised condition dict, or None.
    """
    if not isinstance(condition, dict):
        return None
    binding = condition.get('binding')
    operator = condition.get('operator')
    if not isinstance(binding, str) or not binding.strip():
        return None
    if operator not in _VALID_OPERATORS:
        return None
    threshold_id = condition.get('thresholdId')
    # Key name is 'value', matching Condition.value in rule_models — the
    # importer maps this artefact straight onto the model, so a different key
    # here would silently drop every literal comparand.
    literal = condition.get('value')
    has_threshold = isinstance(threshold_id, str) and bool(threshold_id.strip())
    has_literal = isinstance(literal, str) and bool(literal.strip())
    # Exactly one comparand: both or neither is a malformed condition.
    if has_threshold == has_literal:
        return None
    out: Dict[str, Any] = {'binding': binding.strip(), 'operator': operator}
    if has_threshold:
        out['thresholdId'] = threshold_id.strip()
    else:
        out['value'] = literal.strip()
    return out


def parse_rule_output(raw: str) -> List[Dict[str, Any]]:
    """Parse the model's rule output. Strict — drops anything malformed.

    Never repairs. A rule that survives has: >= 1 valid condition, a non-empty
    outcome IRI, a clamped epistemic status, only source-attested thresholds,
    and a ``missingInputs`` list naming every binding whose threshold did not
    survive. That list is what later routes the rule to advise-rather-than-
    evaluate at query time.

    :param raw: Raw model response text (optionally ```-fenced).
    :returns: A list of normalised rule dicts; empty on any parse failure.
    """
    try:
        payload = json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        return []
    rules = payload.get('rules') if isinstance(payload, dict) else None
    if not isinstance(rules, list):
        return []

    out: List[Dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        scope = rule.get('scope')
        conclusion = rule.get('conclusion')
        if not isinstance(scope, dict) or not isinstance(conclusion, dict):
            continue
        subject_class = scope.get('subjectClass')
        outcome = conclusion.get('outcome')
        if not isinstance(subject_class, str) or not subject_class.strip():
            continue
        if not isinstance(outcome, str) or not outcome.strip():
            continue

        raw_conditions = rule.get('conditions')
        if not isinstance(raw_conditions, list) or not raw_conditions:
            continue
        conditions = [
            parsed
            for parsed in (_parse_condition(c) for c in raw_conditions)
            if parsed is not None
        ]
        # A rule whose conditions did not all survive is not a partially-good
        # rule — it is a DIFFERENT rule from the one the document states, with a
        # clause silently removed. Drop it.
        if len(conditions) != len(raw_conditions):
            continue

        raw_thresholds = rule.get('thresholds')
        thresholds = [
            parsed
            for parsed in (
                _parse_threshold(t)
                for t in (
                    raw_thresholds if isinstance(raw_thresholds, list) else []
                )
            )
            if parsed is not None
        ]
        known_ids = {t['thresholdId'] for t in thresholds}
        # Any condition whose threshold did not survive is a MISSING INPUT, not
        # an error: the rule is real, its bound is simply not stated in the text.
        missing_inputs = [
            c['binding']
            for c in conditions
            if 'thresholdId' in c and c['thresholdId'] not in known_ids
        ]

        source_sentence = rule.get('sourceSentence')
        source_sentence = (
            source_sentence if isinstance(source_sentence, str) else ''
        )
        rule_type = rule.get('ruleType')
        out.append({
            'name': str(rule.get('name', '')).strip(),
            'description': str(rule.get('description', '')).strip(),
            'ruleType': (
                rule_type
                if rule_type in _VALID_RULE_TYPES
                else _DEFAULT_RULE_TYPE
            ),
            'scope': {
                'subjectClass': subject_class.strip(),
                'processStage': str(scope.get('processStage', '')).strip(),
            },
            'conditions': conditions,
            'thresholds': thresholds,
            'conclusion': {
                'outcome': outcome.strip(),
                'epistemicStatus': clamp_epistemic_status(
                    status=str(conclusion.get('epistemicStatus', '')),
                    source_sentence=source_sentence,
                ),
            },
            'missingInputs': missing_inputs,
            'sourceSentence': source_sentence.strip(),
        })
    return out


def _binding_vocabulary(*, entities: List[Dict[str, Any]]) -> List[str]:
    """Collect the linked ontology IRIs available to one chunk.

    The linker sets ``linkedClass`` to an IRI (or None below its cosine
    threshold); only linked entities constrain the model's vocabulary.

    :param entities: The chunk's entities, post-linking.
    :returns: Deduplicated IRIs, order-stable for prompt determinism.
    """
    seen: List[str] = []
    for entity in entities or []:
        iri = entity.get('linkedClass')
        if isinstance(iri, str) and iri and iri not in seen:
            seen.append(iri)
    return seen


def mine_rules(
    *,
    chunk_text: str,
    entities: List[Dict[str, Any]],
    bedrock_runtime: Any,
    model_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run the rule-mining prompt against one chunk.

    :param chunk_text: The chunk body.
    :param entities: The chunk's linked entities (supplies the IRI vocabulary).
    :param bedrock_runtime: A ``bedrock-runtime`` client.
    :param model_id: Override the model; defaults to ``RULE_MINER_MODEL_ID``.
    :returns: Parsed rule dicts (possibly empty).
    """
    if not chunk_text.strip():
        return []
    model_id = model_id or os.environ.get(
        'RULE_MINER_MODEL_ID', 'global.anthropic.claude-sonnet-5'
    )
    vocabulary = _binding_vocabulary(entities=entities)
    user_content = (
        'Ontology IRIs you may use:\n'
        + ('\n'.join(f'- {iri}' for iri in vocabulary) or '- (none linked)')
        + '\n\nText:\n'
        + chunk_text
    )
    # max_tokens matches the NER stage: adaptive thinking shares this OUTPUT
    # budget, and a small cap truncates the rule list mid-emit. No temperature
    # (incompatible with thinking on Sonnet 5).
    body = {
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 4096,
        'system': RULE_MINER_SYSTEM_PROMPT,
        'messages': [{'role': 'user', 'content': user_content}],
    }
    response = bedrock_runtime.invoke_model(
        modelId=model_id,
        body=json.dumps(body).encode('utf-8'),
        contentType='application/json',
        accept='application/json',
    )
    parsed = json.loads(response['body'].read().decode('utf-8'))
    text = ''.join(
        block.get('text', '')
        for block in parsed.get('content', [])
        if block.get('type') == 'text'
    )
    return parse_rule_output(text)


def mine_chunks(
    *, chunks: List[Dict[str, Any]], bedrock_runtime: Any
) -> List[Dict[str, Any]]:
    """Attach a ``rules`` list to each chunk, degrading per chunk on failure.

    :param chunks: The document's chunks, post-linking.
    :param bedrock_runtime: A ``bedrock-runtime`` client.
    :returns: The same chunks with ``rules`` (and ``ruleMinerError`` on failure).
    """
    out: List[Dict[str, Any]] = []
    for chunk in chunks:
        try:
            chunk['rules'] = mine_rules(
                chunk_text=chunk.get('text', ''),
                entities=chunk.get('entities', []),
                bedrock_runtime=bedrock_runtime,
            )
        except Exception as exc:  # noqa: BLE001 — per-chunk, same as NER stage
            logger.warning(
                'rule mining failed for chunk %s: %s', chunk.get('chunkId'), exc
            )
            chunk['rules'] = []
            chunk['ruleMinerError'] = str(exc)
        out.append(chunk)
    return out


def handler(event: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Step Functions handler. Mines rules and writes them to S3 for review.

    Nothing is written to Neptune here: mined rules land as a JSON artefact that
    an admin imports through the REST API's ``:import`` route, so there is
    exactly one Neptune write path and one lifecycle enforcement point.

    :param event: The pipeline event (``chunks`` post-linking, ``documentId``).
    :param context: Unused Lambda context.
    :returns: The event plus ``minedRuleCount`` and ``minedRulesKey``.
    """
    import boto3

    chunks = event.get('chunks', [])
    region = os.environ.get('AWS_REGION', 'us-east-1')
    bedrock_runtime = boto3.client('bedrock-runtime', region_name=region)
    mined = mine_chunks(chunks=chunks, bedrock_runtime=bedrock_runtime)

    all_rules = [rule for chunk in mined for rule in chunk.get('rules', [])]
    document_id = event.get('documentId', 'unknown')
    key = f'mined-rules/{document_id}.json'
    if all_rules:
        s3 = boto3.client('s3', region_name=region)
        s3.put_object(
            Bucket=os.environ['ARTIFACTS_BUCKET'],
            Key=key,
            Body=json.dumps(
                {'documentId': document_id, 'rules': all_rules}, indent=2
            ).encode('utf-8'),
            ContentType='application/json',
        )
    return {
        **event,
        'chunks': mined,
        'minedRuleCount': len(all_rules),
        'minedRulesKey': key if all_rules else '',
    }
