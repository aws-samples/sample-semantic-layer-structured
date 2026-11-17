"""Unit tests for the normalised tables list every ontology build is driven from.

``_build_tables_list`` turns the wizard's raw ``dataSources`` into the entries the prompt
builder bakes into the ontology graph (`vkg:hasCatalog`, `vkg:hasDatabase`). Whatever
coordinate lands here is *persisted*, so a wrong one cannot be corrected at query time —
which is exactly the DynamoDB bug these tests pin.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from ontology_agent import main as ontology_main
from ontology_agent.main import _build_tables_list


def _stub_resolve(monkeypatch, mapping):
    """Replace the _resolve_catalog seam so no test needs boto3 or a Glue table.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        mapping: dict keyed by the incoming (database, table, catalog) triple, valued by the
                 triple _resolve_catalog should return. Any triple not in the mapping comes
                 back unchanged, mirroring the real function's behaviour for non-DDB tables.

    Returns:
        A list that records every (database, table, catalog) the code under test probed, so a
        test can assert a connector catalog is never probed at all.
    """
    probed: list[tuple[str, str, str]] = []

    def _fake(database_name: str, table_name: str, catalog_id: str) -> tuple[str, str, str]:
        probed.append((database_name, table_name, catalog_id))
        return mapping.get(
            (database_name, table_name, catalog_id), (database_name, table_name, catalog_id)
        )

    monkeypatch.setattr(ontology_main, "_resolve_catalog", _fake)
    return probed


def test_glue_coords_passed_through():
    ds = {
        "databaseName": "default",
        "tableName": "semantic-layer-admin-codes",
        "catalogId": "dynamodb_catalog",
        "dataSource": "dynamodb_catalog",
        "tableId": "dynamodb_catalog::default.semantic-layer-admin-codes",
        "glueDatabaseName": "semantic_layer_dynamodb",
        "glueTableName": "semantic_layer_admin_codes",
    }
    result = _build_tables_list([ds])
    assert result[0]["glueDatabaseName"] == "semantic_layer_dynamodb"
    assert result[0]["glueTableName"] == "semantic_layer_admin_codes"


def test_glue_coords_absent_when_not_set():
    ds = {
        "databaseName": "semantic_layer_iceberg",
        "tableName": "admincode",
        "catalogId": "s3tablescatalog/my-bucket",
        "dataSource": "AwsDataCatalog",
        "tableId": "s3tablescatalog/my-bucket::semantic_layer_iceberg.admincode",
    }
    result = _build_tables_list([ds])
    assert result[0].get("glueDatabaseName", "") == ""
    assert result[0].get("glueTableName", "") == ""


def test_ddb_arn_location_reroutes_to_connector_coordinate(monkeypatch):
    """A Glue table whose Location is a DynamoDB ARN must be authored at the connector.

    The admin data-source picker denylists ``dynamodb_catalog`` and surfaces DynamoDB tables
    through the Glue database ``<project>_dynamodb``, so the wizard hands us
    ``catalogId='AWSDataCatalog'`` for a table Athena can read ONLY via the connector. Left
    alone, ``prompt_builder`` bakes ``vkg:hasCatalog "<db>::AWSDataCatalog"`` into the graph
    and every query on that layer dies parsing the ARN as a URI.
    """
    probed = _stub_resolve(
        monkeypatch,
        {
            ("semantic_layer_dev_dynamodb", "semantic_layer_dev_parties", "AWSDataCatalog"): (
                "default",
                "semantic-layer-dev-parties",
                "dynamodb_catalog",
            )
        },
    )
    ds = {
        "databaseName": "semantic_layer_dev_dynamodb",
        "tableName": "semantic_layer_dev_parties",
        "catalogId": "AWSDataCatalog",
        "dataSource": "AwsDataCatalog",
        "tableId": "AWSDataCatalog::semantic_layer_dev_dynamodb.semantic_layer_dev_parties",
    }
    entry = _build_tables_list([ds])[0]

    # The queryable coordinate, which is what gets persisted into the ontology.
    assert entry["catalogId"] == "dynamodb_catalog"
    assert entry["database"] == "default"
    assert entry["table"] == "semantic-layer-dev-parties"
    # A federated connector IS its own data source (cf. glue_service._data_source_for_catalog).
    assert entry["dataSource"] == "dynamodb_catalog"
    # The ORIGINAL Glue coordinate must survive, or column-comment enrichment
    # (update_glue_metadata_from_ontology) can no longer find the real Glue table.
    assert entry["glueDatabaseName"] == "semantic_layer_dev_dynamodb"
    assert entry["glueTableName"] == "semantic_layer_dev_parties"
    assert probed == [
        ("semantic_layer_dev_dynamodb", "semantic_layer_dev_parties", "AWSDataCatalog")
    ]


def test_non_ddb_glue_location_left_unchanged(monkeypatch):
    """A plain-Glue table must be byte-identical to the pre-fix entry.

    This is the regression risk of the whole change: _build_tables_list feeds every build of
    every layer type, so a re-route that fires too eagerly breaks S3 Tables and Snowflake.
    """
    _stub_resolve(monkeypatch, {})  # nothing re-routes
    ds = {
        "databaseName": "normalized",
        "tableName": "party",
        "catalogId": "AWSDataCatalog",
        "dataSource": "AwsDataCatalog",
        "tableId": "AWSDataCatalog::normalized.party",
    }
    entry = _build_tables_list([ds])[0]
    assert entry["catalogId"] == "AWSDataCatalog"
    assert entry["database"] == "normalized"
    assert entry["table"] == "party"
    assert entry["dataSource"] == "AwsDataCatalog"
    # No re-route happened, so no Glue coordinate is synthesised.
    assert entry["glueDatabaseName"] == ""
    assert entry["glueTableName"] == ""


def test_glue_lookup_failure_leaves_coordinate_unchanged(monkeypatch):
    """A Glue probe failure must not drop the table from the build.

    ``_resolve_catalog`` logs and swallows Glue errors, returning the triple unchanged. That
    is the pre-fix behaviour, and it is also what a non-DynamoDB table wants — so the entry
    has to survive rather than the build failing on a transient Glue blip.
    """

    def _raising(database_name: str, table_name: str, catalog_id: str) -> tuple[str, str, str]:
        """Stand-in for _resolve_catalog after its internal except-block swallowed a Glue error."""
        return database_name, table_name, catalog_id

    monkeypatch.setattr(ontology_main, "_resolve_catalog", _raising)
    ds = {
        "databaseName": "semantic_layer_dev_dynamodb",
        "tableName": "semantic_layer_dev_parties",
        "catalogId": "AWSDataCatalog",
        "dataSource": "AwsDataCatalog",
    }
    result = _build_tables_list([ds])
    assert len(result) == 1
    assert result[0]["database"] == "semantic_layer_dev_dynamodb"
    assert result[0]["table"] == "semantic_layer_dev_parties"
    assert result[0]["catalogId"] == "AWSDataCatalog"


def test_connector_catalog_is_never_probed(monkeypatch):
    """An already-correct connector coordinate must cost zero Glue calls.

    _build_tables_list runs once per build over every selected table, so probing a catalog
    that cannot possibly be a Glue table would be pure latency — and it is what keeps the two
    coordinate-passthrough tests above free of boto3.
    """
    probed = _stub_resolve(monkeypatch, {})
    _build_tables_list(
        [
            {
                "databaseName": "default",
                "tableName": "semantic-layer-admin-codes",
                "catalogId": "dynamodb_catalog",
                "glueDatabaseName": "semantic_layer_dynamodb",
                "glueTableName": "semantic_layer_admin_codes",
            },
            {
                "databaseName": "semantic_layer_iceberg",
                "tableName": "admincode",
                "catalogId": "s3tablescatalog/my-bucket",
            },
        ]
    )
    # _resolve_catalog itself no-ops for non-Glue catalogs, so it may be *called*; what must
    # not happen is a Glue round-trip. Assert the stub saw only non-Glue catalogs, which is
    # the branch _resolve_catalog returns from without touching boto3.
    for _db, _tbl, catalog in probed:
        assert catalog not in ("AWSDataCatalog", "AwsDataCatalog", "")
