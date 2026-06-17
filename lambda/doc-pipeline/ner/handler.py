"""NER stage Lambda — self-contained extraction over Bedrock Claude 3.5.

Step Functions invokes this between chunk and embed stages. The Lambda
asset directory is bundled standalone — there is no agents/ tree on the
deployed package, so all NER logic (prompt + parser + Bedrock call) lives
here.

Failed extractions per chunk degrade to entities=[] + nerError so the
linker can skip them without dropping the whole document.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)


NER_SYSTEM_PROMPT = """\
Extract named entities from the supplied text. Output ONLY JSON of the form:

{
  "entities": [
    {"text": "...", "type": "domain_concept | geo | organisation | person | metric | other",
     "span": {"start": <int>, "end": <int>}}
  ]
}

Rules:
- Spans MUST refer to byte offsets in the original input.
- Domain concepts are insurance / financial / business terms.
- Skip stop-word entities (the, of, ...).
- If no entities found, return {"entities": []}.
"""


_VALID_TYPES = frozenset(
    {'domain_concept', 'geo', 'organisation', 'person', 'metric', 'other'}
)


def parse_ner_output(raw: str) -> List[Dict[str, Any]]:
    """Parse the LLM's NER output. Strict — drops malformed entities."""
    text = raw.strip()
    if text.startswith('```'):
        first_newline = text.find('\n')
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    entities = payload.get('entities') if isinstance(payload, dict) else None
    if not isinstance(entities, list):
        return []
    out: List[Dict[str, Any]] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        text_val = ent.get('text')
        type_val = ent.get('type')
        span = ent.get('span')
        if (
            not isinstance(text_val, str)
            or not text_val.strip()
            or type_val not in _VALID_TYPES
            or not isinstance(span, dict)
            or 'start' not in span
            or 'end' not in span
        ):
            continue
        try:
            start = int(span['start'])
            end = int(span['end'])
        except (TypeError, ValueError):
            continue
        out.append({
            'text': text_val.strip(),
            'type': type_val,
            'span': {'start': start, 'end': end},
        })
    return out


def extract_entities(
    *,
    chunk_text: str,
    bedrock_runtime: Any,
    model_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run the NER prompt against one chunk; return parsed entities."""
    if not chunk_text.strip():
        return []
    model_id = model_id or os.environ.get(
        'NER_MODEL_ID', 'global.anthropic.claude-sonnet-4-6'
    )
    body = {
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 1024,
        'system': NER_SYSTEM_PROMPT,
        'messages': [{'role': 'user', 'content': chunk_text}],
    }
    response = bedrock_runtime.invoke_model(
        modelId=model_id,
        body=json.dumps(body).encode('utf-8'),
        contentType='application/json',
        accept='application/json',
    )
    payload_bytes = response['body'].read()
    parsed = json.loads(payload_bytes.decode('utf-8'))
    text_blocks = parsed.get('content', [])
    text = ''.join(
        b.get('text', '') for b in text_blocks if b.get('type') == 'text'
    )
    return parse_ner_output(text)


def annotate_chunks_with_entities(
    *,
    chunks: List[Dict[str, Any]],
    bedrock_runtime: Any,
) -> List[Dict[str, Any]]:
    """Attach an ``entities`` list to each chunk; per-chunk failures degrade
    gracefully so the rest of the pipeline can continue."""
    out: List[Dict[str, Any]] = []
    for chunk in chunks:
        text = chunk.get('text', '')
        try:
            entities = extract_entities(
                chunk_text=text, bedrock_runtime=bedrock_runtime
            )
            chunk['entities'] = entities
        except Exception as exc:  # noqa: BLE001 — per-chunk
            logger.warning(
                'NER failed for chunk %s: %s', chunk.get('chunkId'), exc
            )
            chunk['entities'] = []
            chunk['nerError'] = str(exc)
        out.append(chunk)
    return out


def handler(event: Dict[str, Any], context=None) -> Dict[str, Any]:
    """Step Functions handler."""
    import boto3

    chunks = event.get('chunks', [])
    bedrock_runtime = boto3.client(
        'bedrock-runtime',
        region_name=os.environ.get('AWS_REGION', 'us-east-1'),
    )
    annotated = annotate_chunks_with_entities(
        chunks=chunks, bedrock_runtime=bedrock_runtime
    )
    total_entities = sum(len(c.get('entities', [])) for c in annotated)
    return {
        **event,
        'chunks': annotated,
        'entityCount': total_entities,
    }
