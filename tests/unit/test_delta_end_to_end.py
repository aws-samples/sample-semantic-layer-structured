import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))
from unittest.mock import MagicMock, patch
from services.delta_planner_service import DeltaPlannerService

def _planner_with(dsources, stored):
    svc = DeltaPlannerService(metadata_table=MagicMock(), glue_service=MagicMock(),
                              artifacts_bucket="b", region="us-east-1")
    svc._table.query.return_value = {"Items": [{"version": "v3"}]}
    svc._table.get_item.return_value = {"Item": {
        "id": "L1", "version": "v3", "dataSources": dsources,
        "tableFingerprints": stored}}
    return svc

def test_criteria_1_only_changed_table_in_rebuildset():
    ds = [{"databaseName": "db", "tableName": t, "catalogId": "AWSDataCatalog"}
          for t in ("a", "b", "c")]
    stored = {f"db.{t}": {"schemaHash": "h", "dataMarker": "m"} for t in ("a", "b", "c")}
    svc = _planner_with(ds, stored)
    def fp(dsrc, version):
        h = "hNEW" if dsrc["tableName"] == "b" else "h"
        return {"schemaHash": h, "dataMarker": "m", "markerKind": "ddb_count"}
    with patch.object(svc, "_fingerprint_table", side_effect=fp):
        plan = svc.compute_plan("L1", layer_type="SemanticRAG")
    assert plan["rebuildSet"] == ["db.b"]

def test_criteria_2_data_delta_triggers_rebuild_end_to_end():
    ds = [{"databaseName": "db", "tableName": "a", "catalogId": "AWSDataCatalog"}]
    stored = {"db.a": {"schemaHash": "h", "dataMarker": "mOLD"}}
    svc = _planner_with(ds, stored)
    with patch.object(svc, "_fingerprint_table",
                      return_value={"schemaHash": "h", "dataMarker": "mNEW", "markerKind": "ddb_count"}):
        plan = svc.compute_plan("L1", layer_type="SemanticRAG")
    assert plan["dataChanged"] == ["db.a"]
    assert plan["rebuildSet"] == ["db.a"]
    assert plan["perTable"]["db.a"]["reason"] == "data"

def test_criteria_2_selection_delta_added_and_removed():
    ds = [{"databaseName": "db", "tableName": "new", "catalogId": "AWSDataCatalog"}]
    stored = {"db.gone": {"schemaHash": "h", "dataMarker": "m", "catalogId": "AWSDataCatalog"}}
    svc = _planner_with(ds, stored)
    with patch.object(svc, "_fingerprint_table",
                      return_value={"schemaHash": "h", "dataMarker": "m", "markerKind": "ddb_count"}):
        plan = svc.compute_plan("L1", layer_type="SemanticRAG")
    assert plan["added"] == ["db.new"]
    assert plan["removed"] == ["db.gone"]
    assert "db.new" in plan["rebuildSet"] and "db.gone" not in plan["rebuildSet"]

def test_criteria_3_no_changes_empty_rebuildset():
    ds = [{"databaseName": "db", "tableName": "a", "catalogId": "AWSDataCatalog"}]
    stored = {"db.a": {"schemaHash": "h", "dataMarker": "m"}}
    svc = _planner_with(ds, stored)
    with patch.object(svc, "_fingerprint_table",
                      return_value={"schemaHash": "h", "dataMarker": "m", "markerKind": "ddb_count"}):
        plan = svc.compute_plan("L1", layer_type="SemanticRAG")
    assert plan["rebuildSet"] == []

def test_criteria_5_comment_writeback_is_not_a_delta():
    from agents.shared.delta_fingerprint import schema_hash
    before = [{"name": "id", "type": "string", "comment": ""}]
    after = [{"name": "id", "type": "string", "comment": "AI wrote this"}]
    assert schema_hash(before) == schema_hash(after)

def test_criteria_6_first_run_all_added():
    ds = [{"databaseName": "db", "tableName": "a", "catalogId": "AWSDataCatalog"}]
    svc = _planner_with(ds, {})   # pre-feature layer, no fingerprints
    with patch.object(svc, "_fingerprint_table",
                      return_value={"schemaHash": "h", "dataMarker": "m", "markerKind": "ddb_count"}):
        plan = svc.compute_plan("L1", layer_type="SemanticRAG")
    assert plan["added"] == ["db.a"]
    assert plan["rebuildSet"] == ["db.a"]
