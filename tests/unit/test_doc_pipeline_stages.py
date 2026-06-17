"""Tests for the doc-pipeline embedder, linker, and indexer stages."""

from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# Add each stage's dir to path so the bare ``handler`` module imports work.
_PIPELINE_ROOT = os.path.join(
    os.path.dirname(__file__), '..', '..', 'lambda', 'doc-pipeline'
)
for stage in ('embedder', 'linker', 'indexer'):
    sys.path.insert(0, os.path.join(_PIPELINE_ROOT, stage))


# Each ``handler`` module has the same name; reload as needed in tests.

# ----------------------------------------------------------------------------
# Embedder
# ----------------------------------------------------------------------------


def _import_embedder():
    if 'handler' in sys.modules:
        del sys.modules['handler']
    sys.path.insert(0, os.path.join(_PIPELINE_ROOT, 'embedder'))
    import handler as embedder
    return embedder


def test_embed_chunks_attaches_vectors():
    embedder = _import_embedder()
    runtime = MagicMock()
    runtime.invoke_model.return_value = {
        'body': io.BytesIO(json.dumps({'embedding': [0.1] * 1024}).encode('utf-8'))
    }
    chunks = [{'chunkId': 'c1', 'text': 'hello'}]
    out = embedder.embed_chunks(chunks=chunks, bedrock_runtime=runtime)
    assert out[0]['embedding'] == [0.1] * 1024


def test_embed_chunks_records_per_chunk_failure():
    embedder = _import_embedder()
    runtime = MagicMock()
    runtime.invoke_model.side_effect = RuntimeError('throttled')
    chunks = [{'chunkId': 'c1', 'text': 'hi'}, {'chunkId': 'c2', 'text': 'there'}]
    out = embedder.embed_chunks(chunks=chunks, bedrock_runtime=runtime)
    assert out[0]['embedding'] is None
    assert out[0]['embedError'] == 'throttled'
    assert out[1]['embedding'] is None


def test_embed_chunks_skips_empty_text():
    embedder = _import_embedder()
    runtime = MagicMock()
    out = embedder.embed_chunks(
        chunks=[{'chunkId': 'c', 'text': ''}], bedrock_runtime=runtime
    )
    assert out[0]['embedding'] is None
    assert 'empty text' in out[0]['embedError']
    runtime.invoke_model.assert_not_called()


# ----------------------------------------------------------------------------
# Linker
# ----------------------------------------------------------------------------


def _import_linker():
    if 'handler' in sys.modules:
        del sys.modules['handler']
    sys.path.insert(0, os.path.join(_PIPELINE_ROOT, 'linker'))
    import handler as linker
    return linker


def test_link_entities_assigns_top_class_above_threshold():
    linker = _import_linker()
    entities = [{'text': 'customer', 'embedding': [1.0, 0.0]}]
    class_index = [
        ('ex:Party', [1.0, 0.0]),
        ('ex:Holding', [0.0, 1.0]),
    ]
    out = linker.link_entities(
        entities=entities, class_index=class_index, threshold=0.5
    )
    assert out[0]['linkedClass'] == 'ex:Party'
    assert out[0]['confidence'] >= 0.99


def test_link_entities_below_threshold_returns_none():
    linker = _import_linker()
    entities = [{'text': 'foo', 'embedding': [1.0, 1.0]}]
    class_index = [
        ('ex:Party', [1.0, -1.0]),
    ]
    out = linker.link_entities(
        entities=entities, class_index=class_index, threshold=0.99
    )
    assert out[0]['linkedClass'] is None
    # Confidence is the runner-up score, useful for debugging.
    assert 0.0 <= out[0]['confidence'] <= 1.0


def test_link_entities_handles_empty_embedding():
    linker = _import_linker()
    out = linker.link_entities(
        entities=[{'text': 'x', 'embedding': []}],
        class_index=[('ex:A', [1.0, 0.0])],
    )
    assert out[0]['linkedClass'] is None
    assert out[0]['confidence'] == 0.0


def test_cosine_zero_vector_returns_zero():
    linker = _import_linker()
    assert linker._cosine([0, 0], [1, 1]) == 0.0


# ----------------------------------------------------------------------------
# Indexer
# ----------------------------------------------------------------------------


def _import_indexer():
    if 'handler' in sys.modules:
        del sys.modules['handler']
    sys.path.insert(0, os.path.join(_PIPELINE_ROOT, 'indexer'))
    import handler as indexer
    return indexer


def test_write_chunks_to_s3_uses_jsonl():
    indexer = _import_indexer()
    s3 = MagicMock()
    chunks = [{'chunkId': 'a'}, {'chunkId': 'b'}]
    key = indexer.write_chunks_to_s3(
        chunks=chunks, bucket='bk', prefix='p', s3_client=s3
    )
    assert key == 'p/chunks.jsonl'
    s3.put_object.assert_called_once()
    args = s3.put_object.call_args.kwargs
    assert args['Bucket'] == 'bk'
    assert args['Key'] == 'p/chunks.jsonl'
    assert args['ContentType'] == 'application/x-ndjson'
    body = args['Body'].decode('utf-8')
    assert body.count('\n') == 1


def test_kick_off_ingestion_returns_job_id():
    indexer = _import_indexer()
    bedrock_agent = MagicMock()
    bedrock_agent.start_ingestion_job.return_value = {
        'ingestionJob': {'ingestionJobId': 'job-1'}
    }
    job_id = indexer.kick_off_ingestion(
        knowledge_base_id='kb',
        data_source_id='ds',
        bedrock_agent=bedrock_agent,
    )
    assert job_id == 'job-1'
