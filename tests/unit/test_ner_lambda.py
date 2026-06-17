"""Tests for the NER Step-Functions Lambda's bundled handler.

The Lambda asset directory is bundled standalone, so we verify the
handler module has all the extraction logic inlined and doesn't reach
outside the asset for shared code.
"""

from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_module():
    if 'handler' in sys.modules:
        del sys.modules['handler']
    yield


def _import_handler():
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(__file__),
            '..',
            '..',
            'lambda',
            'doc-pipeline',
            'ner',
        ),
    )
    if 'handler' in sys.modules:
        del sys.modules['handler']
    import handler  # noqa: WPS433
    return handler


def test_handler_module_has_extract_entities_inlined():
    """Critical: the agents/ tree is not bundled with the Lambda asset,
    so the extraction symbols must live directly in handler."""
    handler = _import_handler()
    assert hasattr(handler, 'extract_entities')
    assert hasattr(handler, 'parse_ner_output')
    assert hasattr(handler, 'annotate_chunks_with_entities')
    assert hasattr(handler, 'NER_SYSTEM_PROMPT')


def test_parse_ner_output_well_formed():
    handler = _import_handler()
    raw = (
        '{"entities":[{"text":"Coverage exclusion","type":"domain_concept",'
        '"span":{"start":0,"end":18}}]}'
    )
    out = handler.parse_ner_output(raw)
    assert len(out) == 1
    assert out[0]['type'] == 'domain_concept'


def test_annotate_chunks_records_per_chunk_failure():
    handler = _import_handler()
    runtime = MagicMock()
    runtime.invoke_model.side_effect = RuntimeError('Bedrock throttle')
    chunks = [{'chunkId': 'c1', 'text': 'Florida is hot'}]
    out = handler.annotate_chunks_with_entities(
        chunks=chunks, bedrock_runtime=runtime
    )
    assert out[0]['entities'] == []
    assert out[0]['nerError'] == 'Bedrock throttle'


def test_annotate_chunks_attaches_entities():
    handler = _import_handler()
    runtime = MagicMock()

    def _fresh(**_kwargs):
        return {
            'body': io.BytesIO(json.dumps({
                'content': [{
                    'type': 'text',
                    'text': '{"entities":[{"text":"Florida","type":"geo","span":{"start":0,"end":7}}]}',
                }],
            }).encode('utf-8'))
        }

    runtime.invoke_model.side_effect = _fresh
    out = handler.annotate_chunks_with_entities(
        chunks=[{'chunkId': 'c1', 'text': 'Florida is hot.'}],
        bedrock_runtime=runtime,
    )
    assert out[0]['entities'] == [
        {'text': 'Florida', 'type': 'geo', 'span': {'start': 0, 'end': 7}}
    ]


def test_handler_returns_event_with_entity_count():
    handler = _import_handler()

    fake_runtime = MagicMock()

    def _fresh(**_kwargs):
        return {
            'body': io.BytesIO(json.dumps({
                'content': [{'type': 'text', 'text': '{"entities":[]}'}],
            }).encode('utf-8'))
        }

    fake_runtime.invoke_model.side_effect = _fresh
    with patch('boto3.client', return_value=fake_runtime):
        out = handler.handler(
            {
                'docId': 'd-1',
                'ontologyId': 'o-1',
                'chunks': [{'chunkId': 'c1', 'text': 'hello'}],
            }
        )
    assert out['docId'] == 'd-1'
    assert out['entityCount'] == 0
    assert out['chunks'][0]['entities'] == []
