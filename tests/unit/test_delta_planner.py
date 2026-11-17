import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "rest-api")
)

from unittest.mock import MagicMock, patch
from services.delta_planner_service import DeltaPlannerService


def _svc():
    return DeltaPlannerService(
        metadata_table=MagicMock(),
        glue_service=MagicMock(),
        artifacts_bucket="b",
        region="us-east-1",
    )


def test_fingerprint_iceberg_uses_snapshot_id():
    svc = _svc()
    # Include a partitionKeys list so we exercise the merge_columns path that keeps
    # the planner hash aligned with the agent hash (CR-1).
    svc.glue_service.get_table_metadata.return_value = {
        "columns": [{"name": "id", "type": "string"}],
        "partitionKeys": [{"name": "dt", "type": "date"}],
    }
    with patch.object(svc, "_iceberg_snapshot_id", return_value="99"):
        fp = svc._fingerprint_table(
            {
                "databaseName": "insurance_iceberg",
                "tableName": "holding",
                "catalogId": "s3tablescatalog/bkt",
            },
            version="v3",
        )
    assert fp["markerKind"] == "iceberg_snapshot"
    assert fp["dataMarker"] == "99"
    assert fp["schemaHash"].startswith("sha256:")
    from agents.shared.delta_fingerprint import schema_hash, merge_columns

    assert fp["schemaHash"] == schema_hash(
        merge_columns(
            [{"name": "id", "type": "string"}], [{"name": "dt", "type": "date"}]
        )
    )


def test_fingerprint_ddb_uses_count_marker():
    svc = _svc()
    svc.glue_service.get_table_metadata.return_value = {
        "columns": [{"name": "pk", "type": "string"}]
    }
    with patch.object(svc, "_ddb_count_marker", return_value="rows=123"):
        fp = svc._fingerprint_table(
            {
                "databaseName": "insurance_dynamodb",
                "tableName": "party",
                "catalogId": "AWSDataCatalog",
            },
            version="v3",
        )
    assert fp["markerKind"] == "ddb_count"
    assert fp["dataMarker"] == "rows=123"


def test_diff_buckets_precedence():
    svc = _svc()
    current = {
        "db.t_new": {"schemaHash": "h1", "dataMarker": "m"},
        "db.t_schema": {"schemaHash": "hNEW", "dataMarker": "m"},
        "db.t_data": {"schemaHash": "h", "dataMarker": "mNEW"},
        "db.t_same": {"schemaHash": "h", "dataMarker": "m"},
    }
    stored = {
        "db.t_schema": {"schemaHash": "hOLD", "dataMarker": "m"},
        "db.t_data": {"schemaHash": "h", "dataMarker": "mOLD"},
        "db.t_same": {"schemaHash": "h", "dataMarker": "m"},
        "db.t_gone": {"schemaHash": "h", "dataMarker": "m"},
    }
    b = svc._diff(current, stored)
    assert b["added"] == ["db.t_new"]
    assert b["removed"] == ["db.t_gone"]
    assert b["schemaChanged"] == ["db.t_schema"]
    assert b["dataChanged"] == ["db.t_data"]
    assert b["unchanged"] == ["db.t_same"]


def test_diff_first_run_all_added():
    svc = _svc()
    current = {"db.a": {"schemaHash": "h", "dataMarker": "m"}}
    b = svc._diff(current, {})  # no stored fingerprints (pre-feature layer)
    assert b["added"] == ["db.a"]
    assert b["schemaChanged"] == [] and b["dataChanged"] == []


def test_expand_fk_closure_adds_neighbors_as_fk_neighbors():
    svc = _svc()
    fk_plan = {"coverage": [{"fk_column": "holding_id", "target_table": "holding"}]}
    with patch.object(svc, "_load_last_built_fk_plan", return_value=fk_plan):
        changed = {"holding"}  # bare table names (fragment keys are bare)
        rebuild, neighbors = svc._expand_fk_closure("L1", changed)
    assert "coverage" in rebuild  # inbound neighbor pulled in
    assert "coverage" in neighbors  # tagged so UI shows reason="fk-neighbor"
    assert "holding" not in neighbors  # directly changed, not a neighbor


def test_compute_plan_semantic_rag_end_to_end():
    svc = _svc()
    svc._table.query.return_value = {"Items": [{"version": "v3"}]}
    svc._table.get_item.return_value = {
        "Item": {
            "id": "L1",
            "version": "v3",
            "dataSources": [
                {"databaseName": "db", "tableName": "a", "catalogId": "AWSDataCatalog"},
                {"databaseName": "db", "tableName": "b", "catalogId": "AWSDataCatalog"},
            ],
            "tableFingerprints": {
                "db.a": {"schemaHash": "hOLD", "dataMarker": "m"},
                "db.b": {"schemaHash": "h", "dataMarker": "m"},
            },
        }
    }

    def fake_fp(ds, version):
        return {
            "schemaHash": "hNEW" if ds["tableName"] == "a" else "h",
            "dataMarker": "m",
            "markerKind": "ddb_count",
        }

    with patch.object(svc, "_fingerprint_table", side_effect=fake_fp):
        plan = svc.compute_plan("L1", layer_type="SemanticRAG")
    assert plan["rebuildSet"] == ["db.a"]  # only schema-changed table
    assert plan["unchanged"] == ["db.b"]
    assert plan["perTable"]["db.a"]["reason"] == "schema"
    assert plan["fkNeighbors"] == []  # not VKG


def test_compute_plan_no_changes_empty_rebuildset():
    svc = _svc()
    svc._table.query.return_value = {"Items": [{"version": "v3"}]}
    svc._table.get_item.return_value = {
        "Item": {
            "id": "L1",
            "version": "v3",
            "dataSources": [
                {"databaseName": "db", "tableName": "a", "catalogId": "AWSDataCatalog"}
            ],
            "tableFingerprints": {"db.a": {"schemaHash": "h", "dataMarker": "m"}},
        }
    }
    with patch.object(
        svc,
        "_fingerprint_table",
        return_value={"schemaHash": "h", "dataMarker": "m", "markerKind": "ddb_count"},
    ):
        plan = svc.compute_plan("L1", layer_type="SemanticRAG")
    assert plan["rebuildSet"] == []
