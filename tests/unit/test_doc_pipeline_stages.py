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


# ----------------------------------------------------------------------------
# Indexer — the KB document + sidecar (the no-regression invariant)
# ----------------------------------------------------------------------------


def test_sidecar_carries_no_semantic_layer_version():
    """THE no-regression invariant for the existing SQL path.

    The table-retrieval path filters on
    ``andAll[semantic_layer_id, semantic_layer_version]``. A document chunk with
    no ``semantic_layer_version`` attribute cannot satisfy that filter, so
    documents are provably invisible to the SQL path rather than merely observed
    to be. If this attribute ever appears here, document prose starts competing
    with per-table metadata inside table retrieval — and nothing raises.
    """
    indexer = _import_indexer()
    attrs = json.loads(
        indexer.build_document_sidecar(
            ontology_id='ont-1', doc_id='d1', document_name='policy.pdf'
        )
    )['metadataAttributes']

    assert 'semantic_layer_version' not in attrs
    assert attrs['semantic_layer_id'] == 'ont-1'
    assert attrs['doc_type'] == indexer.DOC_TYPE_DOCUMENT == 'document'
    assert attrs['document_id'] == 'd1'
    assert attrs['document_name'] == 'policy.pdf'


def test_sidecar_doc_type_matches_the_read_side():
    """Agreement test: the write-side discriminator and the read-side filter.

    The indexer and ``agents/shared/document_retrieval`` each declare the
    constant. If they drift, retrieval filters on a value nothing was tagged
    with and every document layer answers nothing, with no error.
    """
    indexer = _import_indexer()
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from agents.shared.document_retrieval import DOC_TYPE_DOCUMENT

    assert indexer.DOC_TYPE_DOCUMENT == DOC_TYPE_DOCUMENT


def test_sidecar_requires_scope_fields():
    """A sidecar without a layer id would be ingested unfiltered (cross-layer leak)."""
    indexer = _import_indexer()
    with pytest.raises(ValueError, match='required'):
        indexer.build_document_sidecar(
            ontology_id='', doc_id='d1', document_name='f.pdf')
    with pytest.raises(ValueError, match='required'):
        indexer.build_document_sidecar(
            ontology_id='ont-1', doc_id='', document_name='f.pdf')


def test_sidecar_over_the_kb_size_limit_raises():
    """Over the limit Bedrock drops the whole document silently, so fail loudly."""
    indexer = _import_indexer()
    with pytest.raises(ValueError, match='silently dropped'):
        indexer.build_document_sidecar(
            ontology_id='ont-1', doc_id='d1',
            document_name='x' * (indexer.KB_METADATA_MAX_BYTES + 1),
        )


def test_kb_document_key_lives_under_the_ingested_prefix():
    """The key must sit under ``metadata/`` — the data source's inclusionPrefix.

    And under ``_documents/`` where a version segment would be, so a document can
    never collide with a ``metadata/{id}/{version}/…`` table document.
    """
    indexer = _import_indexer()
    key = indexer.kb_document_key(ontology_id='ont-1', doc_id='d1')
    assert key == 'metadata/ont-1/_documents/d1.md'


def test_render_document_markdown_puts_a_citation_in_every_heading():
    """Each section heading names the file and location, so a retrieved chunk
    carries its own citation without a second lookup."""
    indexer = _import_indexer()
    body = indexer.render_document_markdown(
        chunks=[
            {'text': 'Flood is excluded.', 'sourceLocation': {'type': 'pdf', 'page': 12}},
            {'text': 'Wind is covered.', 'sourceLocation': {'section': 'Coverage'}},
        ],
        document_name='policy.pdf',
    )
    assert '# policy.pdf' in body
    assert '## [policy.pdf — page 12]' in body
    assert '## [policy.pdf — Coverage]' in body
    assert 'Flood is excluded.' in body


def test_render_document_markdown_raises_when_no_chunk_has_text():
    """An empty KB document ingests cleanly and answers nothing — the exact
    silent failure this path exists to remove."""
    indexer = _import_indexer()
    with pytest.raises(ValueError, match='carries any text'):
        indexer.render_document_markdown(
            chunks=[{'text': '  '}, {'text': ''}], document_name='empty.pdf')


def test_write_kb_document_validates_the_sidecar_before_writing_anything():
    """A pathological filename must not orphan a ``.md`` with no sidecar.

    A sidecar-less object is ingested unfiltered, which leaks the document into
    every layer's retrieval — strictly worse than not ingesting it at all.
    """
    indexer = _import_indexer()
    s3 = MagicMock()
    with pytest.raises(ValueError):
        indexer.write_kb_document(
            chunks=[{'text': 'body'}],
            ontology_id='ont-1',
            doc_id='d1',
            document_name='y' * (indexer.KB_METADATA_MAX_BYTES + 1),
            bucket='bk',
            s3_client=s3,
        )
    s3.put_object.assert_not_called()


def test_write_kb_document_writes_the_markdown_and_its_sidecar():
    """Both objects land, with the sidecar keyed as ``<key>.metadata.json``."""
    indexer = _import_indexer()
    s3 = MagicMock()
    key = indexer.write_kb_document(
        chunks=[{'text': 'Flood is excluded.', 'sourceLocation': {'type': 'pdf', 'page': 3}}],
        ontology_id='ont-1',
        doc_id='d1',
        document_name='policy.pdf',
        bucket='bk',
        s3_client=s3,
    )
    assert key == 'metadata/ont-1/_documents/d1.md'
    written = {c.kwargs['Key']: c.kwargs for c in s3.put_object.call_args_list}
    assert set(written) == {key, f"{key}.metadata.json"}
    assert written[key]['ContentType'] == 'text/markdown'
    assert written[f"{key}.metadata.json"]['ContentType'] == 'application/json'
    sidecar = json.loads(written[f"{key}.metadata.json"]['Body'])
    assert sidecar['metadataAttributes']['doc_type'] == 'document'
