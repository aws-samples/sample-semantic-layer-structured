# tests/unit/test_delta_fingerprint.py
from unittest.mock import MagicMock, patch

import agents.shared.delta_fingerprint as df
from agents.shared.delta_fingerprint import (
    make_fingerprint,
    merge_columns,
    normalize_type,
    read_stored_fingerprints,
    schema_hash,
    write_table_fingerprint,
)


def test_normalize_type_aliases_equal():
    assert normalize_type("varchar") == normalize_type("string")
    assert normalize_type("int") == normalize_type("integer")
    assert normalize_type("VARCHAR(20)") == normalize_type("string")


def test_schema_hash_ignores_comment_text():
    # Same columns/types, different AI-written comments -> SAME hash.
    a = [{"name": "id", "type": "string", "comment": ""}]
    b = [{"name": "id", "type": "string", "comment": "AI-written description"}]
    assert schema_hash(a) == schema_hash(b)


def test_schema_hash_changes_on_new_column():
    a = [{"name": "id", "type": "string"}]
    b = [{"name": "id", "type": "string"}, {"name": "amt", "type": "double"}]
    assert schema_hash(a) != schema_hash(b)


def test_schema_hash_changes_on_column_type_change():
    # Same column name, type changes string -> int: this IS a real delta.
    a = [{"name": "id", "type": "string"}]
    b = [{"name": "id", "type": "int"}]
    assert schema_hash(a) != schema_hash(b)


def test_schema_hash_order_independent_of_dict_key_order():
    a = [{"type": "string", "name": "id"}]
    b = [{"name": "id", "type": "string"}]
    assert schema_hash(a) == schema_hash(b)


def test_normalize_type_preserves_decimal_precision():
    # Precision/scale is semantic — different p,s must NOT collapse.
    assert normalize_type("decimal(10,2)") != normalize_type("decimal(38,0)")
    # Whitespace + case are cosmetic — these MUST collapse.
    assert normalize_type("DECIMAL(10, 2)") == normalize_type("decimal(10,2)")


def test_normalize_type_preserves_complex_element_type():
    # Element type is semantic — int vs string must NOT collapse.
    assert normalize_type("array<int>") != normalize_type("array<string>")
    # Inner alias is cosmetic — varchar collapses to string inside the array.
    assert normalize_type("array<varchar>") == normalize_type("array<string>")


def test_normalize_type_empty_and_none():
    # Must not raise on empty / None input.
    assert normalize_type("") == ""
    assert normalize_type(None) == ""


def test_merge_columns_appends_partition_keys_in_order():
    # THE INVARIANT: the planner (GlueService: columns + partitionKeys as SEPARATE
    # lists) must produce the SAME column set the agents hash (get_single_table_schema
    # APPENDS partition keys into `columns`). merge_columns is the single canonical
    # order both sides use: data columns first, then partition keys.
    cols = [{"name": "id", "type": "string"}]
    pks = [{"name": "dt", "type": "date"}]
    merged = merge_columns(cols, pks)
    assert [c["name"] for c in merged] == ["id", "dt"]


def test_planner_and_agent_hash_agree_with_partition_keys():
    # Planner side: GlueService returns columns + partitionKeys separately.
    planner_cols = merge_columns(
        [{"name": "id", "type": "string"}],  # meta["columns"]
        [{"name": "dt", "type": "date"}],  # meta["partitionKeys"]
    )
    # Agent side: get_single_table_schema already appended the partition key.
    agent_cols = [{"name": "id", "type": "string"}, {"name": "dt", "type": "date"}]
    assert schema_hash(planner_cols) == schema_hash(agent_cols)


def test_make_fingerprint_shape():
    fp = make_fingerprint(
        schema_h="sha256:x",
        data_marker="42",
        marker_kind="iceberg_snapshot",
        version="v3",
        catalog_id="AWSDataCatalog",
    )
    assert fp["schemaHash"] == "sha256:x"
    assert fp["dataMarker"] == "42"
    assert fp["markerKind"] == "iceberg_snapshot"
    assert fp["builtInVersion"] == "v3"
    assert fp["catalogId"] == "AWSDataCatalog"  # needed for removal cleanup coords
    assert "builtAt" in fp


def test_write_table_fingerprint_uses_two_step_map_update():
    # Two separate UpdateItem calls: (1) init parent map if absent, (2) set the
    # nested child path. A SINGLE expression that touches both `tableFingerprints`
    # and `tableFingerprints.<key>` is rejected by DynamoDB (overlapping paths),
    # so this MUST be two calls — see test_write_table_fingerprint_persists_moto.
    table = MagicMock()
    write_table_fingerprint(
        table,
        layer_id="L1",
        version="v3",
        db_table="db.t",
        fp={"schemaHash": "sha256:x"},
    )
    assert table.update_item.call_count == 2
    init_kwargs, set_kwargs = (c.kwargs for c in table.update_item.call_args_list)
    # Step 1 — parent-map init (no overlap with the child path).
    assert init_kwargs["Key"] == {"id": "L1", "version": "v3"}
    assert "if_not_exists(tableFingerprints" in init_kwargs["UpdateExpression"]
    assert "tableFingerprints.#dt" not in init_kwargs["UpdateExpression"]
    # Step 2 — nested child SET so we never clobber sibling tables' fingerprints.
    assert "tableFingerprints.#dt" in set_kwargs["UpdateExpression"]
    assert set_kwargs["ExpressionAttributeNames"]["#dt"] == "db.t"


def test_write_table_fingerprint_persists_moto():
    """Regression guard for C1: prove the write actually persists against a real
    (moto) DynamoDB and that a second table does not clobber the first. The old
    single-expression form threw ValidationException here; a MagicMock hid it."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="meta",
            KeySchema=[
                {"AttributeName": "id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        tbl = ddb.Table("meta")
        tbl.put_item(Item={"id": "L1", "version": "v3"})

        write_table_fingerprint(tbl, layer_id="L1", version="v3",
                                db_table="db.a", fp={"schemaHash": "hA"})
        write_table_fingerprint(tbl, layer_id="L1", version="v3",
                                db_table="db.b", fp={"schemaHash": "hB"})

        stored = tbl.get_item(Key={"id": "L1", "version": "v3"})["Item"]["tableFingerprints"]
        assert stored == {"db.a": {"schemaHash": "hA"}, "db.b": {"schemaHash": "hB"}}


def test_read_stored_fingerprints_defaults_empty():
    assert read_stored_fingerprints({}) == {}
    assert read_stored_fingerprints({"tableFingerprints": {"db.t": {}}}) == {"db.t": {}}


def test_ddb_count_marker_combines_itemcount_and_athena():
    with patch("boto3.client") as bc:
        ddb = MagicMock()
        ddb.describe_table.return_value = {"Table": {"ItemCount": 500}}
        bc.return_value = ddb
        with patch.object(df, "_athena_scalar", return_value="512"):
            marker = df.ddb_count_marker(
                db="db",
                tbl="party",
                catalog_id="AWSDataCatalog",
                region="us-east-1",
                artifacts_bucket="b",
            )
    assert "items=500" in marker and "count=512" in marker  # both signals


def test_ddb_count_marker_survives_athena_failure():
    with patch("boto3.client") as bc:
        ddb = MagicMock()
        ddb.describe_table.return_value = {"Table": {"ItemCount": 7}}
        bc.return_value = ddb
        with patch.object(df, "_athena_scalar", side_effect=RuntimeError("boom")):
            marker = df.ddb_count_marker(
                db="db",
                tbl="party",
                catalog_id="AWSDataCatalog",
                region="us-east-1",
                artifacts_bucket="b",
            )
    assert "items=7" in marker and "count=NA" in marker  # fail-soft
