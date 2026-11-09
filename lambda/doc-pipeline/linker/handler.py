"""Doc-pipeline linker (item #3).

Maps NER-extracted entity surface text to ontology classes via cosine
similarity over the same Bedrock Titan embeddings the chunker uses.

The class index (IRI → embedding) is built from Bedrock KB results in
production; the unit tests inject a fixed index so the cosine logic is
exercised without AWS.

Threshold for accepting a link: 0.80 (per design doc).

A layer can legitimately have **no** class index — a document-only Semantic RAG
layer has no ontology to link entities against. That case is an explicitly
recorded degradation (``classIndexMissing`` in the stage output), not a silent
fallback, and it is distinguished from a *configured but broken* index, which
raises. See ``load_class_index``.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Sequence, Tuple

logger = logging.getLogger(__name__)

LINK_THRESHOLD = float(os.environ.get('DOC_PIPELINE_LINK_THRESHOLD', '0.80'))

# S3 key of the per-layer class index, formatted with the event's ontologyId.
# Overridable so a steward can relocate the artifact without a code change.
DEFAULT_CLASS_INDEX_KEY_TEMPLATE = 'ontologies/{ontology_id}/class-index.json'


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    if len(a) != len(b):
        raise ValueError("vector length mismatch")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def link_entities(
    *,
    entities: List[Dict[str, Any]],
    class_index: List[Tuple[str, Sequence[float]]],
    threshold: float = LINK_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Attach ``linkedClass`` and ``confidence`` to each entity.

    ``class_index`` is a list of ``(iri, embedding_vector)`` tuples. We use a
    list-of-tuples rather than a dict because IRIs may collide on hash but
    we want a deterministic top-1 selection on score.

    Args:
        entities: Each must already carry an ``embedding`` vector.
        class_index: Reference (IRI, embedding) pairs.
        threshold: Minimum cosine score to count as a link.

    Returns:
        The same entity list with linkage fields populated. Entities below
        threshold get ``linkedClass=None`` and ``confidence`` set to the
        runner-up score for debuggability.
    """
    out: List[Dict[str, Any]] = []
    for entity in entities:
        emb = entity.get('embedding')
        if not emb:
            entity['linkedClass'] = None
            entity['confidence'] = 0.0
            out.append(entity)
            continue
        scored: List[Tuple[str, float]] = []
        for iri, class_emb in class_index:
            try:
                score = _cosine(emb, class_emb)
            except ValueError:
                continue
            scored.append((iri, score))
        if not scored:
            entity['linkedClass'] = None
            entity['confidence'] = 0.0
            out.append(entity)
            continue
        scored.sort(key=lambda x: x[1], reverse=True)
        top_iri, top_score = scored[0]
        if top_score >= threshold:
            entity['linkedClass'] = top_iri
            entity['confidence'] = round(float(top_score), 4)
        else:
            entity['linkedClass'] = None
            entity['confidence'] = round(float(top_score), 4)
        out.append(entity)
    return out


def resolve_class_index_key(*, ontology_id: str) -> str:
    """Resolve the S3 key of a layer's class index.

    ``CLASS_INDEX_S3_KEY`` (a literal key) wins when set, so an operator can
    point one pipeline at a specific artifact. Otherwise
    ``CLASS_INDEX_S3_KEY_TEMPLATE`` is formatted with ``ontology_id``.

    Args:
        ontology_id: The semantic-layer id from the pipeline event.

    Returns:
        The S3 object key to read the class index from.

    Raises:
        ValueError: When ``ontology_id`` is empty — a per-layer key cannot be
            resolved without it, and guessing would link a document's entities
            against another layer's ontology.
    """
    literal_key = os.environ.get('CLASS_INDEX_S3_KEY')
    if literal_key:
        return literal_key
    if not ontology_id:
        raise ValueError(
            "cannot resolve the class-index key: the event carries no ontologyId "
            "and CLASS_INDEX_S3_KEY is not set"
        )
    template = os.environ.get(
        'CLASS_INDEX_S3_KEY_TEMPLATE', DEFAULT_CLASS_INDEX_KEY_TEMPLATE
    )
    return template.format(ontology_id=ontology_id)


def load_class_index(
    *, bucket: str, key: str, s3_client: Any
) -> Tuple[List[Tuple[str, Sequence[float]]], bool]:
    """Load a class index from S3, distinguishing "absent" from "broken".

    Args:
        bucket: Artifacts bucket name.
        key: Object key resolved by :func:`resolve_class_index_key`.
        s3_client: A boto3 S3 client (injected so tests need no AWS).

    Returns:
        ``(class_index, missing)``. When the object does not exist, returns
        ``([], True)``: a document-only layer has no ontology to link against,
        so an empty index is the correct answer and every entity ends up with
        ``linkedClass=None``.

    Raises:
        RuntimeError: When the object exists but cannot be read or parsed into
            ``[{iri, embedding}, ...]``. A configured-but-broken index is a bug,
            and silently treating it as "absent" would hide it — entities would
            all come back unlinked and the pipeline would report success.
    """
    import json

    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)['Body'].read()
    except Exception as exc:  # noqa: BLE001 — narrowed immediately below
        # botocore raises a dynamically-built ClientError subclass, so match on
        # the error code rather than the exception type.
        code = getattr(exc, 'response', {}).get('Error', {}).get('Code', '')
        if code in ('NoSuchKey', 'NotFound', '404'):
            logger.warning(
                'No class index at s3://%s/%s — entities will be left unlinked',
                bucket, key,
            )
            return [], True
        raise

    try:
        loaded = json.loads(body.decode('utf-8'))
        class_index = [(it['iri'], it['embedding']) for it in loaded]
    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError(
            f"class index at s3://{bucket}/{key} exists but is not a valid "
            f"[{{iri, embedding}}, ...] JSON array: {exc}"
        ) from exc
    return class_index, False


def handler(event: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Step Functions handler.

    The class index is supplied inline in the event (integration/unit tests) or
    read from the artifacts bucket at the per-layer key. An absent index is
    recorded as ``classIndexMissing`` rather than failing the stage, so a
    document-only layer completes the pipeline.

    Args:
        event: Previous stage's output; must carry ``entities`` and
            ``ontologyId``. An inline ``classIndex`` takes precedence.
        context: Unused Lambda context.

    Returns:
        The event with ``entities`` linked and ``classIndexMissing`` set.
    """
    import boto3

    entities = event.get('entities', [])
    class_index_inline = event.get('classIndex')
    if class_index_inline:
        class_index: List[Tuple[str, Sequence[float]]] = [
            (item['iri'], item['embedding']) for item in class_index_inline
        ]
        missing = False
    else:
        bucket = os.environ['ARTIFACTS_BUCKET']
        key = resolve_class_index_key(ontology_id=event.get('ontologyId', ''))
        s3 = boto3.client('s3', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
        class_index, missing = load_class_index(bucket=bucket, key=key, s3_client=s3)

    linked = link_entities(entities=entities, class_index=class_index)
    return {**event, 'entities': linked, 'classIndexMissing': missing}
