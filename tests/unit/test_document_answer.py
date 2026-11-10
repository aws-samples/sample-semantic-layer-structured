"""Unit tests for ``agents/shared/document_answer`` — the prose-answer terminal.

The properties under test are the ones that make it safe to hand a model some
retrieved prose and present the result as an answer:

- **No SQL, structurally.** ``as_dict`` always carries an empty ``executed_sql``
  and empty ``results``; the module is handed a ``synthesize`` callable and has no
  Athena client to misuse.
- **Grounding is enforced on the way out.** Citations the model returns are
  checked against the passages it was actually given. An answer citing nothing
  supplied is downgraded to insufficient rather than presented with fabricated
  provenance.
- **``try_document_answer`` returns None or a grounded answer — never a partial
  one**, so every caller's existing abstain path stays the fallback. This is the
  single seam all three agents call, so its contract is what keeps VKG,
  Semantic RAG and Hybrid from drifting.
- The ``document`` tier is registered everywhere a tier must be registered
  (``build_provenance`` RAISES on an unknown tier, so an unregistered tier 500s
  every document-answered turn).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from agents.shared.document_answer import (  # noqa: E402
    DOCUMENT_TIER,
    MAX_PASSAGE_CHARS,
    DocumentAnswer,
    answer_from_documents,
    try_document_answer,
)
from agents.shared.document_retrieval import DocumentChunk  # noqa: E402


def _chunk(
    *,
    document_id: str = 'd1',
    citation: str = 'policy.pdf — page 3',
    text: str = '## [policy.pdf — page 3]\nFlood damage is excluded.',
) -> DocumentChunk:
    """A retrieved passage as ``retrieve_document_chunks`` returns it."""
    return DocumentChunk(
        document_id=document_id,
        document_name='policy.pdf',
        text=text,
        score=0.8,
        citation=citation,
    )


def _model(payload) -> object:
    """A synthesize stub returning ``payload`` (dict → JSON, str → verbatim).

    Records the prompt it was handed on ``.prompts`` so tests can assert on what
    the model actually saw.
    """
    class _Stub:
        def __init__(self):
            self.prompts: list = []

        def __call__(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return payload if isinstance(payload, str) else json.dumps(payload)

    return _Stub()


# ── answer_from_documents: the happy path ────────────────────────────────────


def test_grounded_answer_is_sufficient_with_verified_sources():
    synth = _model({
        'sufficient': True,
        'answer': 'No — flood is excluded [policy.pdf — page 3].',
        'citations': ['policy.pdf — page 3'],
    })
    out = answer_from_documents(
        question='is flood covered?', chunks=[_chunk()], synthesize=synth)

    assert out.sufficient is True
    assert out.citations == ['policy.pdf — page 3']
    # Sources follow the existing `rule:<id>` convention and carry the passage
    # ordinal, so a source traces to the exact passage, not merely the document.
    assert out.sources == ['doc:d1#0']
    assert out.knowledge_gap is None


def test_as_dict_can_never_report_sql():
    """The no-SQL guarantee is visible in the payload, not only in the prompt."""
    payload = DocumentAnswer(
        answer='a', citations=['c'], sources=['doc:d1#0'], sufficient=True).as_dict()
    assert payload['tier'] == DOCUMENT_TIER == 'document'
    assert payload['executed_sql'] == ''
    assert payload['results'] == []


def test_prompt_carries_the_passages_and_strips_their_inner_headings():
    """The citation label is already the passage prefix; leaving the '## [..]'
    heading in as well invites the model to cite a mid-passage heading as the
    whole source."""
    synth = _model({'sufficient': False, 'answer': '', 'knowledge_gap': 'x'})
    answer_from_documents(question='q?', chunks=[_chunk()], synthesize=synth)

    prompt = synth.prompts[0]
    assert 'q?' in prompt
    assert '[policy.pdf — page 3]\nFlood damage is excluded.' in prompt
    assert '## [policy.pdf — page 3]' not in prompt


def test_oversized_passage_is_truncated():
    """Guards against one pathological chunk blowing the model's context."""
    synth = _model({'sufficient': False, 'answer': '', 'knowledge_gap': 'x'})
    answer_from_documents(
        question='q',
        chunks=[_chunk(text='z' * (MAX_PASSAGE_CHARS * 3))],
        synthesize=synth,
    )
    assert synth.prompts[0].count('z') <= MAX_PASSAGE_CHARS


# ── answer_from_documents: every abstain route ───────────────────────────────


def test_no_passages_abstains_without_calling_the_model():
    """Asking the model with nothing to ground in is how an answer gets invented."""
    synth = _model({'sufficient': True, 'answer': 'made up', 'citations': []})
    out = answer_from_documents(question='q', chunks=[], synthesize=synth)

    assert out.sufficient is False
    assert synth.prompts == []
    assert out.knowledge_gap == 'no document passage matched the question'


def test_model_saying_insufficient_is_respected():
    synth = _model({
        'sufficient': False, 'answer': '', 'knowledge_gap': 'no flood section'})
    out = answer_from_documents(question='q', chunks=[_chunk()], synthesize=synth)
    assert out.sufficient is False
    assert out.knowledge_gap == 'no flood section'


def test_unparseable_response_abstains():
    out = answer_from_documents(
        question='q', chunks=[_chunk()], synthesize=_model('not json at all'))
    assert out.sufficient is False
    assert 'parsed' in (out.knowledge_gap or '')


def test_fenced_json_is_tolerated():
    """Models wrap JSON in a code fence often enough that rejecting it would
    abstain on otherwise-good answers."""
    raw = '```json\n{"sufficient": true, "answer": "Yes [policy.pdf — page 3].",' \
          ' "citations": ["policy.pdf — page 3"]}\n```'
    out = answer_from_documents(
        question='q', chunks=[_chunk()], synthesize=_model(raw))
    assert out.sufficient is True
    assert out.sources == ['doc:d1#0']


def test_synthesis_failure_abstains_rather_than_erroring_the_turn():
    def _boom(prompt: str) -> str:
        raise RuntimeError('model unavailable')

    out = answer_from_documents(question='q', chunks=[_chunk()], synthesize=_boom)
    assert out.sufficient is False
    assert 'synthesis failed' in (out.knowledge_gap or '')


def test_an_invented_citation_downgrades_the_answer():
    """THE grounding guard: a citation pointing at nothing reads as provenance.

    The model claims sufficiency and cites a page it was never shown. Presenting
    that answer would attach believable-looking provenance to an ungrounded claim.
    """
    synth = _model({
        'sufficient': True,
        'answer': 'Yes, up to $50,000 [policy.pdf — page 99].',
        'citations': ['policy.pdf — page 99'],
    })
    out = answer_from_documents(question='q', chunks=[_chunk()], synthesize=synth)

    assert out.sufficient is False
    assert out.sources == []
    assert out.knowledge_gap == 'the answer could not be grounded in a cited passage'


def test_partially_invented_citations_keep_only_the_supplied_ones():
    synth = _model({
        'sufficient': True,
        'answer': 'Excluded [policy.pdf — page 3], and also [guide.pdf — page 1].',
        'citations': ['policy.pdf — page 3', 'guide.pdf — page 1'],
    })
    out = answer_from_documents(question='q', chunks=[_chunk()], synthesize=synth)
    assert out.sufficient is True
    assert out.citations == ['policy.pdf — page 3']
    assert out.sources == ['doc:d1#0']


def test_sufficient_but_empty_answer_abstains():
    synth = _model({'sufficient': True, 'answer': '   ', 'citations': ['policy.pdf — page 3']})
    out = answer_from_documents(question='q', chunks=[_chunk()], synthesize=synth)
    assert out.sufficient is False


def test_multiple_passages_get_distinct_ordinals():
    chunks = [
        _chunk(document_id='d1', citation='a.pdf — page 1', text='first'),
        _chunk(document_id='d2', citation='b.pdf — page 9', text='second'),
    ]
    synth = _model({
        'sufficient': True,
        'answer': 'Both [a.pdf — page 1] [b.pdf — page 9].',
        'citations': ['b.pdf — page 9', 'a.pdf — page 1'],
    })
    out = answer_from_documents(question='q', chunks=chunks, synthesize=synth)
    assert out.sources == ['doc:d2#1', 'doc:d1#0']


# ── try_document_answer: the seam all three agents call ──────────────────────


class _FakeKb:
    """A bedrock-agent-runtime stand-in yielding one document chunk, or none."""

    def __init__(self, *, results: list):
        self._results = results
        self.calls: list = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return {'retrievalResults': self._results}


def _kb_result() -> dict:
    return {
        'content': {'text': '## [policy.pdf — page 3]\nFlood damage is excluded.'},
        'metadata': {
            'doc_type': 'document', 'document_id': 'd1', 'document_name': 'policy.pdf'},
        'score': 0.8,
    }


def _good_model():
    return _model({
        'sufficient': True,
        'answer': 'No — flood is excluded [policy.pdf — page 3].',
        'citations': ['policy.pdf — page 3'],
    })


def test_seam_returns_a_grounded_answer():
    kb = _FakeKb(results=[_kb_result()])
    out = try_document_answer(
        question='is flood covered?', kb_id='kb-1', layer_id='ont-1',
        synthesize=_good_model(), kb_client=kb)

    assert out is not None
    assert out.sufficient is True
    assert out.sources == ['doc:d1#0']


@pytest.mark.parametrize('kb_id, layer_id', [('', 'ont-1'), ('kb-1', '')])
def test_seam_returns_none_when_unconfigured_without_touching_the_kb(kb_id, layer_id):
    """No KB (enableSemanticRag=false) or no resolved layer is not an error —
    there are simply no documents to fall back to."""
    kb = _FakeKb(results=[_kb_result()])
    assert try_document_answer(
        question='q', kb_id=kb_id, layer_id=layer_id,
        synthesize=_good_model(), kb_client=kb) is None
    assert kb.calls == []


def test_seam_returns_none_when_nothing_is_retrieved():
    """A layer with no documents keeps its caller's existing abstain, unchanged."""
    synth = _good_model()
    assert try_document_answer(
        question='q', kb_id='kb-1', layer_id='ont-1',
        synthesize=synth, kb_client=_FakeKb(results=[])) is None
    # And costs no model call.
    assert synth.prompts == []


def test_seam_returns_none_rather_than_an_insufficient_answer():
    """None, never a partial answer: the caller must not be able to accidentally
    present an abstention as a document-grounded answer."""
    synth = _model({'sufficient': False, 'answer': 'maybe', 'knowledge_gap': 'gap'})
    assert try_document_answer(
        question='q', kb_id='kb-1', layer_id='ont-1',
        synthesize=synth, kb_client=_FakeKb(results=[_kb_result()])) is None


def test_seam_returns_none_when_the_answer_is_ungrounded():
    synth = _model({
        'sufficient': True, 'answer': 'Yes.', 'citations': ['nowhere.pdf — page 1']})
    assert try_document_answer(
        question='q', kb_id='kb-1', layer_id='ont-1',
        synthesize=synth, kb_client=_FakeKb(results=[_kb_result()])) is None


def test_seam_forwards_top_k():
    kb = _FakeKb(results=[])
    try_document_answer(
        question='q', kb_id='kb-1', layer_id='ont-1',
        synthesize=_good_model(), kb_client=kb, top_k=3)
    cfg = kb.calls[0]['retrievalConfiguration']['vectorSearchConfiguration']
    assert cfg['numberOfResults'] == 3


# ── Tier registration ────────────────────────────────────────────────────────


def test_the_document_tier_is_accepted_by_build_provenance():
    """``build_provenance`` RAISES on an unknown tier, so an unregistered tier
    would 500 every document-answered turn rather than degrade."""
    from agents.shared.provenance import VALID_TIERS, build_provenance

    assert DOCUMENT_TIER in VALID_TIERS
    prov = build_provenance(tier=DOCUMENT_TIER, sources=['doc:d1#0'])
    assert prov['tier'] == 'document'
    assert prov['sources'] == ['doc:d1#0']
    # A document answer is grounded (in cited passages) and did not degrade — the
    # SQL path's degrade reason is reported in reasoning.dataSourceSelection, not
    # here, because `degraded` drives the "this turn failed" badge.
    assert prov['grounded'] is True
    assert prov['degraded'] is None


def test_all_three_agents_call_the_same_seam():
    """The consistency requirement, asserted on source text.

    Three separate call sites is where per-layer-type behaviour drifts back in.
    Asserting each one calls ``try_document_answer`` — rather than reimplementing
    retrieve-then-answer — is the cheapest guard that they cannot.
    """
    root = os.path.join(os.path.dirname(__file__), '..', '..', 'agents')
    for agent in (
        'metadata_query_agent',      # Semantic RAG
        'ontology_query_agent',      # VKG
        'semantic_layer_query_agent',  # Hybrid
    ):
        with open(os.path.join(root, agent, 'main.py'), encoding='utf-8') as fh:
            src = fh.read()
        assert 'try_document_answer(' in src, f"{agent} does not call the shared seam"
        # And must not hand-roll retrieval around it.
        assert 'retrieve_document_chunks(' not in src, (
            f"{agent} retrieves documents itself instead of via the shared seam")


def test_hybrid_copy_of_the_answer_module_is_byte_identical():
    """The Hybrid container ships only its own package, so this module is COPIED.
    A divergent copy is the drift this change exists to remove."""
    root = os.path.join(os.path.dirname(__file__), '..', '..', 'agents')
    with open(os.path.join(root, 'shared', 'document_answer.py'), 'rb') as fh:
        shared = fh.read()
    with open(
        os.path.join(root, 'semantic_layer_query_agent', 'document_answer.py'), 'rb'
    ) as fh:
        hybrid = fh.read()
    assert shared == hybrid
