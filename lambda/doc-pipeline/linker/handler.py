"""Doc-pipeline linker (item #3).

Maps NER-extracted entity surface text to ontology classes via cosine
similarity over the same Bedrock Titan embeddings the chunker uses.

The class index (IRI → embedding) is built from Bedrock KB results in
production; the unit tests inject a fixed index so the cosine logic is
exercised without AWS.

Threshold for accepting a link: 0.80 (per design doc).
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LINK_THRESHOLD = float(os.environ.get('DOC_PIPELINE_LINK_THRESHOLD', '0.80'))


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


def handler(event: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Step Functions handler. The class index is loaded from S3 (path in
    env var ``CLASS_INDEX_S3_KEY``) or supplied inline in the event for
    integration tests."""
    import json

    import boto3

    entities = event.get('entities', [])
    class_index_inline = event.get('classIndex')
    if class_index_inline:
        class_index = [(item['iri'], item['embedding']) for item in class_index_inline]
    else:
        bucket = os.environ['ARTIFACTS_BUCKET']
        key = os.environ['CLASS_INDEX_S3_KEY']
        s3 = boto3.client('s3', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
        body = s3.get_object(Bucket=bucket, Key=key)['Body'].read()
        loaded = json.loads(body.decode('utf-8'))
        class_index = [(it['iri'], it['embedding']) for it in loaded]
    linked = link_entities(entities=entities, class_index=class_index)
    return {**event, 'entities': linked}
