"""
Unit tests for delta-scope wiring in MetadataService.start_metadata_enrichment.

A delta build (target_tables set) must persist deltaTargetTables into the config
record so the metadata agent can scope its loop. A full build (no target_tables)
must REMOVE any stale deltaTargetTables using a valid DynamoDB UpdateExpression
(all SET clauses first, then a single trailing REMOVE).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))

from unittest.mock import MagicMock, patch


def _make_service():
    """Construct a MetadataService with boto3 + AgentCoreService patched out.

    Mirrors the construction-mocking pattern in test_metadata_service.py so the
    __init__ boto3 resource / AgentCoreService calls do not hit AWS.

    Returns:
        A MetadataService instance safe to use in unit tests.
    """
    with patch('services.metadata_service.boto3'), \
         patch('services.metadata_service.AgentCoreService'):
        from services.metadata_service import MetadataService
        return MetadataService()


def test_delta_scope_writes_delta_targets_to_config():
    """A delta build binds :delta = list(target_tables) in the update expression."""
    svc = _make_service()
    svc._metadata_table = MagicMock()
    svc.agentcore_service = MagicMock()
    svc._get_latest_metadata_item = MagicMock(return_value={
        "id": "L1", "version": "v3",
        "dataSources": [
            {"databaseName": "db", "tableName": "a", "catalogId": "AWSDataCatalog"},
            {"databaseName": "db", "tableName": "b", "catalogId": "AWSDataCatalog"},
        ],
    })
    svc.start_metadata_enrichment("L1", target_tables=["db.a"])
    upd = svc._metadata_table.update_item.call_args.kwargs
    assert ":delta" in upd["ExpressionAttributeValues"]
    assert upd["ExpressionAttributeValues"][":delta"] == ["db.a"]


def test_full_build_clears_delta_targets_with_valid_expression():
    """A full build appends a single trailing REMOVE deltaTargetTables clause."""
    svc = _make_service()
    svc._metadata_table = MagicMock()
    svc.agentcore_service = MagicMock()
    svc._get_latest_metadata_item = MagicMock(return_value={
        "id": "L1", "version": "v3",
        "dataSources": [{"databaseName": "db", "tableName": "a", "catalogId": "AWSDataCatalog"}],
    })
    svc.start_metadata_enrichment("L1")   # no target_tables → full build
    expr = svc._metadata_table.update_item.call_args.kwargs["UpdateExpression"]
    assert expr.count("SET") == 1
    assert expr.count("REMOVE") == 1
    assert expr.index("SET") < expr.index("REMOVE")
    assert "REMOVE deltaTargetTables" in expr
    assert ":delta" not in svc._metadata_table.update_item.call_args.kwargs["ExpressionAttributeValues"]


def test_compute_delta_plan_delegates_to_planner():
    """compute_delta_plan wires a DeltaPlannerService and delegates for SemanticRAG."""
    svc = _make_service()
    svc._metadata_table = MagicMock()
    with patch("services.metadata_service.DeltaPlannerService") as DP:
        DP.return_value.compute_plan.return_value = {"rebuildSet": ["db.a"]}
        plan = svc.compute_delta_plan("L1")
    assert plan["rebuildSet"] == ["db.a"]
    DP.return_value.compute_plan.assert_called_once_with("L1", layer_type="SemanticRAG")


def test_cleanup_removed_resolves_catalog_from_stored_fingerprint():
    """Removed tables resolve their catalog from the STORED fingerprint (catalogId)."""
    svc = _make_service()
    svc._metadata_table = MagicMock()
    svc._get_latest_metadata_item = MagicMock(return_value={
        "id": "L1", "version": "v7",
        "tableFingerprints": {"db.gone": {"catalogId": "s3tablescatalog/bkt"}},
    })
    with patch("services.metadata_service.DeltaPlannerService") as DP:
        svc.cleanup_removed_for_plan("L1", {"removed": ["db.gone"]})
    kwargs = DP.return_value.cleanup_removed.call_args.kwargs
    assert kwargs["version"] == "v7"
    assert kwargs["layer_type"] == "SemanticRAG"
    assert kwargs["removed"] == [("db", "gone", "s3tablescatalog/bkt")]


def test_cleanup_removed_defaults_catalog_and_warns_when_no_fingerprint(caplog):
    """A removed table with no stored fingerprint defaults to AWSDataCatalog and warns."""
    svc = _make_service()
    svc._metadata_table = MagicMock()
    svc._get_latest_metadata_item = MagicMock(return_value={"id": "L1", "version": "v2"})
    with patch("services.metadata_service.DeltaPlannerService") as DP:
        with caplog.at_level("WARNING"):
            svc.cleanup_removed_for_plan("L1", {"removed": ["db.orphan"]})
    kwargs = DP.return_value.cleanup_removed.call_args.kwargs
    assert kwargs["removed"] == [("db", "orphan", "AWSDataCatalog")]
    assert any("no stored fingerprint" in r.message for r in caplog.records)


def test_cleanup_removed_noop_when_nothing_removed():
    """An empty removed list does not construct a planner or call cleanup."""
    svc = _make_service()
    svc._metadata_table = MagicMock()
    with patch("services.metadata_service.DeltaPlannerService") as DP:
        svc.cleanup_removed_for_plan("L1", {"removed": []})
    DP.assert_not_called()


from services.ontology_service import OntologyService


def test_ontology_delta_plan_uses_vkg_layer_type():
    with patch('services.ontology_service.boto3'), \
         patch('services.ontology_service.AgentCoreService'):
        svc = OntologyService()
    svc.table = MagicMock()
    with patch("services.ontology_service.DeltaPlannerService") as DP:
        DP.return_value.compute_plan.return_value = {"rebuildSet": ["db.a"], "fkNeighbors": []}
        svc.compute_delta_plan("L1")
    DP.return_value.compute_plan.assert_called_once_with("L1", layer_type="VKG")


def test_ontology_build_writes_delta_targets():
    with patch('services.ontology_service.boto3'), \
         patch('services.ontology_service.AgentCoreService'):
        svc = OntologyService()
    svc.table = MagicMock()
    svc.agentcore_service = MagicMock()
    svc.get_metadata_config = MagicMock(return_value={
        "id": "L1", "version": "v3",
        "dataSources": [{"databaseName": "db", "tableName": "a", "catalogId": "AWSDataCatalog"}],
    })
    svc.start_build_metadata_async("L1", target_tables=["db.a"])
    saved = svc.table.put_item.call_args.kwargs["Item"]
    assert saved["deltaTargetTables"] == ["db.a"]
