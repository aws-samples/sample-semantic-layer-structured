"""Unit tests for ``agents/shared/document_retrieval``.

The read side of the doc-pipeline indexer's write contract. What these pin:

- The retrieval filter is ``andAll[semantic_layer_id, doc_type=document]`` — the
  layer scope, because an unfiltered retrieve returns another layer's documents,
  and the ``doc_type`` predicate, because without it the 24 pre-existing per-table
  metadata documents come back as if they were uploaded prose.
- The filter does NOT mention ``semantic_layer_version``: the doc sidecar carries
  none, so retrieving on it would match nothing. (The mirror assertion — that the
  sidecar omits it — lives in test_doc_pipeline_stages.)
- Retrieval is fail-soft, because this module runs after the SQL attempt has
  already failed; raising would turn a graceful abstain into a 500.
- The Hybrid agent's copy is byte-identical, which is what keeps the three layer
  types from drifting.
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from agents.shared.document_retrieval import (  # noqa: E402
    DEFAULT_TOP_K,
    DOC_TYPE_DOCUMENT,
    MAX_TOP_K,
    DocumentChunk,
    extract_citation,
    retrieve_document_chunks,
)


class _FakeKb:
    """A bedrock-agent-runtime stand-in returning scripted retrieval results."""

    def __init__(self, *, results: list, raises: bool = False):
        self._results = results
        self._raises = raises
        self.calls: list = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise RuntimeError('KB unavailable')
        return {'retrievalResults': self._results}


def _result(
    *,
    text: str = '## [policy.pdf — page 3]\nFlood is excluded.',
    doc_type: str = DOC_TYPE_DOCUMENT,
    document_id: str = 'd1',
    document_name: str = 'policy.pdf',
    score: float = 0.7,
) -> dict:
    """One KB retrievalResults entry with a document sidecar's attributes."""
    meta = {'document_id': document_id, 'document_name': document_name}
    if doc_type is not None:
        meta['doc_type'] = doc_type
    return {'content': {'text': text}, 'metadata': meta, 'score': score}


# ── The filter ───────────────────────────────────────────────────────────────


def test_filter_scopes_by_layer_and_doc_type():
    """Both predicates, ANDed. Either one missing is a silent correctness bug."""
    kb = _FakeKb(results=[_result()])
    retrieve_document_chunks(
        kb_id='kb-1', layer_id='ont-1', question='is flood covered?', client=kb)

    cfg = kb.calls[0]['retrievalConfiguration']['vectorSearchConfiguration']
    assert cfg['filter'] == {
        'andAll': [
            {'equals': {'key': 'semantic_layer_id', 'value': 'ont-1'}},
            {'equals': {'key': 'doc_type', 'value': 'document'}},
        ]
    }
    assert kb.calls[0]['knowledgeBaseId'] == 'kb-1'
    assert kb.calls[0]['retrievalQuery'] == {'text': 'is flood covered?'}


def test_filter_never_mentions_semantic_layer_version():
    """Doc sidecars carry no version attribute, so filtering on one matches nothing."""
    kb = _FakeKb(results=[_result()])
    retrieve_document_chunks(kb_id='kb-1', layer_id='ont-1', question='q', client=kb)
    assert 'semantic_layer_version' not in str(kb.calls[0])


def test_top_k_defaults_and_clamps_to_the_api_limit():
    """numberOfResults must stay inside Bedrock's 1..100 or the call ValidationErrors."""
    kb = _FakeKb(results=[])
    retrieve_document_chunks(kb_id='kb', layer_id='l', question='q', client=kb)
    cfg = kb.calls[0]['retrievalConfiguration']['vectorSearchConfiguration']
    assert cfg['numberOfResults'] == DEFAULT_TOP_K

    retrieve_document_chunks(
        kb_id='kb', layer_id='l', question='q', client=kb, top_k=10_000)
    assert kb.calls[1]['retrievalConfiguration'][
        'vectorSearchConfiguration']['numberOfResults'] == MAX_TOP_K

    retrieve_document_chunks(kb_id='kb', layer_id='l', question='q', client=kb, top_k=0)
    assert kb.calls[2]['retrievalConfiguration'][
        'vectorSearchConfiguration']['numberOfResults'] == 1


# ── Programming errors raise; retrieval errors do not ────────────────────────


@pytest.mark.parametrize(
    'kwargs, match',
    [
        ({'kb_id': '', 'layer_id': 'l'}, 'kb_id is required'),
        ({'kb_id': 'kb', 'layer_id': ''}, 'layer_id is required'),
    ],
)
def test_missing_scope_raises(kwargs, match):
    """An unscoped retrieve would cross layers, so this is an error, not a miss."""
    with pytest.raises(ValueError, match=match):
        retrieve_document_chunks(question='q', client=_FakeKb(results=[]), **kwargs)


def test_missing_client_raises():
    """The client is injected precisely so the Hybrid copy needs no boto import."""
    with pytest.raises(ValueError, match='client'):
        retrieve_document_chunks(
            kb_id='kb', layer_id='l', question='q', client=None)


def test_retrieval_error_is_fail_soft(caplog):
    """A KB outage yields no chunks, so the caller keeps its existing abstain."""
    kb = _FakeKb(results=[], raises=True)
    with caplog.at_level(logging.ERROR):
        assert retrieve_document_chunks(
            kb_id='kb', layer_id='ont-1', question='q', client=kb) == []
    # Fail-soft, never fail-silent.
    assert any('ont-1' in r.getMessage() for r in caplog.records)


# ── Result mapping ───────────────────────────────────────────────────────────


def test_maps_results_to_document_chunks_with_citations():
    kb = _FakeKb(results=[_result()])
    chunks = retrieve_document_chunks(
        kb_id='kb', layer_id='l', question='q', client=kb)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert isinstance(chunk, DocumentChunk)
    assert chunk.document_id == 'd1'
    assert chunk.document_name == 'policy.pdf'
    assert chunk.score == pytest.approx(0.7)
    # The citation is recovered from the heading the indexer wrote into the text,
    # so it survives retrieval without a second lookup.
    assert chunk.citation == 'policy.pdf — page 3'


def test_a_chunk_without_doc_type_is_dropped():
    """Defence in depth against a sidecar-less object the server filter missed.

    A per-table metadata document handed to the prose prompt would be answered
    from as if a human had uploaded it — wrong, and invisible.
    """
    kb = _FakeKb(results=[_result(doc_type=None), _result(document_id='d2')])
    chunks = retrieve_document_chunks(
        kb_id='kb', layer_id='l', question='q', client=kb)
    assert [c.document_id for c in chunks] == ['d2']


def test_empty_text_results_are_dropped():
    """A blank passage contributes nothing but dilutes the prompt."""
    kb = _FakeKb(results=[_result(text='   '), _result(document_id='d2')])
    chunks = retrieve_document_chunks(
        kb_id='kb', layer_id='l', question='q', client=kb)
    assert [c.document_id for c in chunks] == ['d2']


def test_no_results_returns_empty_list():
    kb = _FakeKb(results=[])
    assert retrieve_document_chunks(
        kb_id='kb', layer_id='l', question='q', client=kb) == []


# ── Citation parsing ─────────────────────────────────────────────────────────


def test_extract_citation_reads_the_first_heading():
    text = '## [guide.pdf — page 12]\nbody\n## [guide.pdf — page 13]\nmore'
    assert extract_citation(text=text, document_name='guide.pdf') == 'guide.pdf — page 12'


def test_extract_citation_falls_back_to_the_document_name():
    """HIERARCHICAL chunking can split mid-section, leaving no heading."""
    assert extract_citation(
        text='body with no heading', document_name='guide.pdf') == 'guide.pdf'


# ── Cross-package agreement ──────────────────────────────────────────────────


def test_hybrid_copy_is_byte_identical():
    """The Hybrid container ships only its own package (see test_hybrid_isolation),
    so this module is COPIED there rather than imported. A divergent copy is
    exactly the per-layer-type drift this whole change removes."""
    root = os.path.join(os.path.dirname(__file__), '..', '..', 'agents')
    with open(os.path.join(root, 'shared', 'document_retrieval.py'), 'rb') as fh:
        shared = fh.read()
    with open(
        os.path.join(root, 'semantic_layer_query_agent', 'document_retrieval.py'), 'rb'
    ) as fh:
        hybrid = fh.read()
    assert shared == hybrid
