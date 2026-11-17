"""Unit tests for the REST build dispatcher's Hybrid routing.

``OntologyService.dispatch_build`` reads the config ``type`` and routes to the
correct per-type build entrypoint. These tests guard two things:

1. ``type: "Hybrid"`` reaches the new ``start_build_hybrid_async`` path.
2. ``type: "VKG"`` still reaches the existing metadata builder and does NOT
   leak into the hybrid path — protecting the existing VKG/SemanticRAG modes
   from a dispatch regression.
3. Both routes fan out to document ingestion first — the whole point of putting
   it in the dispatcher is that no layer type can skip it.
"""

import os
import sys

# Add lambda/rest-api directory to sys.path so we can import services — matches
# the existing REST unit-test convention (see test_ontology_service_versioning).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))

from services.ontology_service import OntologyService  # noqa: E402


def test_build_dispatch_routes_hybrid():
    """A Hybrid config routes to ``start_build_hybrid_async`` only."""
    svc = OntologyService.__new__(OntologyService)
    called = {}
    svc.get_metadata_config = lambda i: {"id": i, "type": "Hybrid", "dataSources": [{}]}
    svc.start_build_hybrid_async = lambda i: called.setdefault("hybrid", i)
    # Stub the VKG/SemanticRAG path so a mis-route is detectable. Accept the
    # delta kwargs the real signature now takes so a mis-route raises AssertionError
    # (wrong destination), not TypeError (wrong signature).
    svc.start_build_metadata_async = lambda i, **kw: called.setdefault("metadata", i)
    # Stub the document fan-out: the real one builds a DocumentService and would
    # reach DynamoDB. Its own behaviour is covered by test_ontology_service_doc_fanout.
    svc._start_document_ingestion = lambda **kw: called.setdefault("docs", kw)

    svc.dispatch_build("o")

    assert called.get("hybrid") == "o"
    assert "metadata" not in called
    # Documents are ingested for Hybrid too, not just for the metadata modes.
    assert called.get("docs") == {"ontology_id": "o"}


def test_build_dispatch_routes_vkg_not_hybrid():
    """A VKG config routes to the existing metadata builder, never to hybrid."""
    svc = OntologyService.__new__(OntologyService)
    called = {}
    svc.get_metadata_config = lambda i: {"id": i, "type": "VKG", "dataSources": [{}]}
    svc.start_build_hybrid_async = lambda i: called.setdefault("hybrid", i)
    # Capture the forwarded delta kwargs so we can assert dispatch_build passes
    # them through to the VKG/SemanticRAG builder unchanged.
    svc.start_build_metadata_async = lambda i, **kw: called.update(
        {"metadata": i, "kw": kw})
    svc._start_document_ingestion = lambda **kw: called.setdefault("docs", kw)

    svc.dispatch_build("o", target_tables=["db.t1"], removed_tables=["db.t2"])

    assert called.get("metadata") == "o"
    assert "hybrid" not in called
    assert called.get("docs") == {"ontology_id": "o"}
    # Delta scope must reach the builder (regression guard for the rebase merge of
    # hybrid's dispatch_build with main's delta-scoped build params).
    assert called["kw"] == {"target_tables": ["db.t1"], "removed_tables": ["db.t2"]}
