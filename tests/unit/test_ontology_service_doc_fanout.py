"""Unit tests for the build-time document fan-out.

``OntologyService.dispatch_build`` re-runs the doc pipeline for every document
uploaded to the layer before it dispatches to the per-type builder. These tests
pin the three properties that make that safe and uniform:

1. Every document gets one execution, with the payload shape the Chunk stage
   expects and a UNIQUE execution name (Step Functions rejects a duplicate name
   for 90 days, so a shared name would silently no-op every rebuild).
2. One failing document does not block the build, nor the remaining documents.
3. The fan-out runs for all three layer types — VKG, SemanticRAG and Hybrid —
   which is the consistency requirement the dispatcher placement exists to meet.
"""

from __future__ import annotations

import os
import sys

import pytest

# Make the rest-api package importable — matches the existing REST test convention.
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))

from services.document_service import DocumentService  # noqa: E402
from services.ontology_service import OntologyService  # noqa: E402


class _FakeSfn:
    """Records start_execution calls; optionally fails for named documents."""

    def __init__(self, *, fail_for: tuple = ()):
        self.calls: list = []
        self._fail_for = fail_for

    def start_execution(self, **kwargs):
        self.calls.append(kwargs)
        if any(f in kwargs['name'] for f in self._fail_for):
            raise RuntimeError('simulated Step Functions failure')
        return {'executionArn': f"arn:aws:states:::execution/{kwargs['name']}"}


def _doc(doc_id: str, *, filename: str = 'policy.pdf') -> dict:
    """A document status row as ``list_documents`` returns it."""
    return {
        'docId': doc_id,
        'ontologyId': 'ont-1',
        'filename': filename,
        's3Bucket': 'artifacts',
        's3Key': f"supplementary-docs/ont-1/raw/{doc_id}/{filename}",
    }


def _service(*, docs: list, sfn: _FakeSfn) -> OntologyService:
    """An OntologyService whose document service lists ``docs`` and uses ``sfn``.

    Built with ``__new__`` (no boto clients) — the same approach as
    test_hybrid_build_dispatch — with a real DocumentService so the tests
    exercise the production ``start_pipeline`` payload rather than a stub of it.
    """
    doc_svc = DocumentService(
        table_name='t', bucket='artifacts', region='us-east-1',
        state_machine_arn='arn:aws:states:::stateMachine:doc-pipeline',
        sfn_client=sfn,
    )
    doc_svc.list_documents = lambda *, ontology_id: docs  # type: ignore[assignment]

    svc = OntologyService.__new__(OntologyService)
    svc._document_service = doc_svc
    return svc


def test_every_document_starts_one_execution_with_the_chunk_payload():
    """Each doc yields one execution carrying docId/ontologyId/bucket/key/filename."""
    sfn = _FakeSfn()
    svc = _service(docs=[_doc('d1'), _doc('d2', filename='notes.md')], sfn=sfn)

    summary = svc._start_document_ingestion(ontology_id='ont-1')

    assert summary == {'documents': 2, 'started': 2, 'failed': 0}
    assert len(sfn.calls) == 2

    import json
    payload = json.loads(sfn.calls[0]['input'])
    assert payload == {
        'docId': 'd1',
        'ontologyId': 'ont-1',
        's3Bucket': 'artifacts',
        's3Key': 'supplementary-docs/ont-1/raw/d1/policy.pdf',
        'filename': 'policy.pdf',
    }


def test_execution_names_are_unique_per_build():
    """Two builds of the same document must not collide on execution name.

    Step Functions rejects a duplicate execution name for 90 days with
    ExecutionAlreadyExists, so a stable name would make every rebuild's
    re-ingestion a silent no-op — the failure mode this test exists to prevent.
    """
    sfn = _FakeSfn()
    svc = _service(docs=[_doc('d1')], sfn=sfn)

    svc._start_document_ingestion(ontology_id='ont-1')
    svc._start_document_ingestion(ontology_id='ont-1')

    names = [c['name'] for c in sfn.calls]
    assert len(set(names)) == 2, f"execution names collided: {names}"
    # Still traceable back to the document, and within the 80-char SFN limit.
    assert all('d1' in n and len(n) <= 80 for n in names)


def test_one_failing_document_does_not_block_the_others_or_the_build():
    """A failure is counted and logged; the remaining documents still start."""
    sfn = _FakeSfn(fail_for=('bad',))
    svc = _service(docs=[_doc('bad'), _doc('good')], sfn=sfn)

    summary = svc._start_document_ingestion(ontology_id='ont-1')

    assert summary == {'documents': 2, 'started': 1, 'failed': 1}
    assert len(sfn.calls) == 2  # the good doc was still attempted


def test_no_documents_is_a_no_op():
    """A layer with no uploads starts nothing and reports zeros (no crash)."""
    sfn = _FakeSfn()
    svc = _service(docs=[], sfn=sfn)

    assert svc._start_document_ingestion(ontology_id='ont-1') == {
        'documents': 0, 'started': 0, 'failed': 0}
    assert sfn.calls == []


def test_a_failing_list_documents_does_not_block_the_build():
    """DynamoDB trouble degrades to "no documents", never to a failed build."""
    def _boom(*, ontology_id: str):
        raise RuntimeError('DDB unavailable')

    sfn = _FakeSfn()
    svc = _service(docs=[], sfn=sfn)
    svc._document_service.list_documents = _boom  # type: ignore[assignment]

    assert svc._start_document_ingestion(ontology_id='ont-1') == {
        'documents': 0, 'started': 0, 'failed': 0}


@pytest.mark.parametrize('layer_type', ['VKG', 'SemanticRAG', 'Hybrid'])
def test_fanout_runs_for_every_layer_type(layer_type: str):
    """The consistency requirement: no layer type can skip document ingestion."""
    sfn = _FakeSfn()
    svc = _service(docs=[_doc('d1')], sfn=sfn)
    svc.get_metadata_config = lambda i: {  # type: ignore[assignment]
        'id': i, 'type': layer_type, 'dataSources': [{}]}
    svc.start_build_hybrid_async = lambda i: {'status': 'pending'}  # type: ignore[assignment]
    svc.start_build_metadata_async = lambda i, **kw: {'status': 'pending'}  # type: ignore[assignment]

    svc.dispatch_build('ont-1')

    assert len(sfn.calls) == 1, f"{layer_type} did not ingest its documents"


def test_unknown_layer_type_ingests_nothing_and_still_raises():
    """An unknown type fails as before, without starting orphan executions."""
    sfn = _FakeSfn()
    svc = _service(docs=[_doc('d1')], sfn=sfn)
    svc.get_metadata_config = lambda i: {'id': i, 'type': 'Nonsense'}  # type: ignore[assignment]

    with pytest.raises(ValueError, match='Unknown build type'):
        svc.dispatch_build('ont-1')
    assert sfn.calls == []


def test_start_pipeline_raises_when_no_state_machine_is_configured(monkeypatch):
    """A mis-wired deployment surfaces instead of leaving docs un-indexed.

    Clear the env var so the empty ctor arg cannot fall through to a value
    another test (or the developer's shell) has set — the ctor resolves
    ``state_machine_arn or os.environ['DOC_PIPELINE_STATE_MACHINE_ARN']``.
    """
    monkeypatch.delenv('DOC_PIPELINE_STATE_MACHINE_ARN', raising=False)
    doc_svc = DocumentService(
        table_name='t', bucket='b', region='us-east-1',
        state_machine_arn='', sfn_client=_FakeSfn(),
    )
    with pytest.raises(RuntimeError, match='DOC_PIPELINE_STATE_MACHINE_ARN'):
        doc_svc.start_pipeline(
            ontology_id='ont-1', doc_id='d1', s3_bucket='b',
            s3_key='k', filename='f.txt',
        )
