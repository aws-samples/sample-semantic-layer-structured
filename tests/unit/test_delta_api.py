import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))
from unittest.mock import patch
from fastapi.testclient import TestClient
import metadata_api
import ontology_api


def test_delta_endpoint_returns_plan():
    client = TestClient(metadata_api.app)
    with patch.object(metadata_api.metadata_service, "compute_delta_plan",
                      return_value={"rebuildSet": ["db.a"], "removed": []}):
        r = client.post("/delta/L1")
    assert r.status_code == 200
    assert r.json()["rebuildSet"] == ["db.a"]


def test_enrich_delta_scope_runs_cleanup_then_build():
    client = TestClient(metadata_api.app)
    with patch.object(metadata_api.metadata_service, "cleanup_removed_for_plan") as cr, \
         patch.object(metadata_api.metadata_service, "start_metadata_enrichment",
                      return_value={"jobId": "L1", "status": "processing"}):
        r = client.post("/enrich", json={
            "id": "L1", "targetTables": ["db.a"],
            "removedTables": ["db.gone"], "scope": "delta"})
    assert r.status_code == 202
    assert cr.called


def test_ontology_delta_endpoint():
    client = TestClient(ontology_api.app)
    with patch.object(ontology_api.ontology_service, "compute_delta_plan",
                      return_value={"rebuildSet": ["db.a"], "fkNeighbors": ["db.b"]}):
        r = client.post("/delta/L1")
    assert r.status_code == 200
    assert r.json()["fkNeighbors"] == ["db.b"]


def test_ontology_build_passes_scope_targets():
    client = TestClient(ontology_api.app)
    # The /build route now dispatches through ``dispatch_build``, which first reads
    # the layer ``type`` via ``get_metadata_config`` and then forwards the delta
    # scope params to ``start_build_metadata_async`` for VKG/SemanticRAG layers.
    # Mock the config lookup (VKG type) so dispatch reaches the metadata builder.
    with patch.object(ontology_api.ontology_service, "get_metadata_config",
                      return_value={"id": "L1", "type": "VKG"}), \
         patch.object(ontology_api.ontology_service, "start_build_metadata_async",
                      return_value={"id": "L1", "status": "pending"}) as sb:
        r = client.post("/build/L1", json={
            "scope": "delta", "targetTables": ["db.a"], "removedTables": ["db.gone"]})
    assert r.status_code in (200, 202)
    kwargs = sb.call_args.kwargs
    assert kwargs["target_tables"] == ["db.a"]
    assert kwargs["removed_tables"] == ["db.gone"]
