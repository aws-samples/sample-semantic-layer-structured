"""Unit tests for the DynamoDB-connector query-coordinate derivation.

A Glue table backed by the Athena DynamoDB connector must be QUERIED at the
connector coordinate (catalog=<connector>, db='default', table=<ARN tail>), not
the Glue DESCRIBE coordinate. ``_ddb_query_coordinate`` derives it from the Glue
``StorageDescriptor.Location`` ARN; ``save_metadata_document_to_s3`` writes it into
the KB sidecar so the query agent's SQL executes instead of failing SCHEMA_NOT_FOUND.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.metadata_agent import main
from agents.metadata_agent.main import _ddb_query_coordinate


def _session_with_location(location):
    """A boto session whose glue.get_table returns the given StorageDescriptor.Location."""
    fake_glue = MagicMock()
    fake_glue.get_table.return_value = {
        "Table": {"StorageDescriptor": {"Location": location}}
    }
    fake_session = MagicMock()
    fake_session.client.return_value = fake_glue
    return fake_session


def test_derives_connector_coordinate_from_ddb_arn():
    sess = _session_with_location(
        "arn:aws:dynamodb:us-east-1:381492284087:table/semantic-layer-dev-parties"
    )
    coord = _ddb_query_coordinate(sess, "semantic_layer_dev_dynamodb",
                                  "semantic_layer_dev_parties", "dynamodb_catalog")
    assert coord == ("dynamodb_catalog", "default", "semantic-layer-dev-parties")


def test_non_ddb_location_returns_none():
    # An S3 location (normalized Iceberg / plain Glue) is NOT a DDB-connector table.
    sess = _session_with_location("s3://some-bucket/warehouse/party/")
    assert _ddb_query_coordinate(sess, "normalized", "party",
                                 "s3tablescatalog/x") is None


def test_empty_or_missing_location_returns_none():
    assert _ddb_query_coordinate(_session_with_location(""), "d", "t", "c") is None
    # A non-str Location (e.g. a stray mock / malformed Glue record) must NOT be
    # treated as a DDB ARN — guards against a truthy mock .startswith.
    assert _ddb_query_coordinate(_session_with_location(MagicMock()), "d", "t", "c") is None


def test_malformed_ddb_arn_raises():
    # A DDB ARN with no ':table/' tail is a real data problem → fail loud.
    sess = _session_with_location("arn:aws:dynamodb:us-east-1:381492284087:")
    with pytest.raises(ValueError):
        _ddb_query_coordinate(sess, "d", "t", "dynamodb_catalog")


def test_glue_miss_returns_none_not_raises():
    # A Glue get_table failure must not block the save — return None (no derivation).
    fake_glue = MagicMock()
    fake_glue.get_table.side_effect = RuntimeError("EntityNotFound")
    fake_session = MagicMock()
    fake_session.client.return_value = fake_glue
    assert _ddb_query_coordinate(fake_session, "d", "t", "c") is None


def _capture_s3(monkeypatch, location):
    """Stub S3 + bucket env; glue.get_table returns the given Location."""
    fake_s3 = MagicMock()
    fake_glue = MagicMock()
    fake_glue.get_table.return_value = {
        "Table": {"StorageDescriptor": {"Location": location}}
    }

    def _client(name, *a, **k):
        return fake_glue if name == "glue" else fake_s3

    fake_session = MagicMock()
    fake_session.client.side_effect = _client
    monkeypatch.setattr(main, "get_boto_session", lambda: fake_session)
    monkeypatch.setenv("ARTIFACTS_BUCKET", "test-bucket")
    return fake_s3


def _sidecar_attrs(fake_s3):
    for call in fake_s3.put_object.call_args_list:
        if call.kwargs["Key"].endswith(".metadata.json"):
            return json.loads(call.kwargs["Body"])["metadataAttributes"]
    raise AssertionError("no .metadata.json put_object call captured")


def _doc_body(fake_s3):
    for call in fake_s3.put_object.call_args_list:
        if call.kwargs["Key"].endswith(".md") and not call.kwargs["Key"].endswith(".metadata.json"):
            return call.kwargs["Body"].decode("utf-8")
    raise AssertionError("no .md put_object call captured")


def test_sidecar_records_connector_coordinate_for_ddb_table(monkeypatch):
    fake_s3 = _capture_s3(
        monkeypatch,
        "arn:aws:dynamodb:us-east-1:381492284087:table/semantic-layer-dev-parties",
    )
    main.save_metadata_document_to_s3(
        database_name="semantic_layer_dev_dynamodb",
        table_name="semantic_layer_dev_parties",
        catalog_id="dynamodb_catalog",
        metadata_content=(
            "# dynamodb_catalog.semantic_layer_dev_dynamodb.semantic_layer_dev_parties\n\n"
            "## Overview\nParties.\n\n## Common Query Patterns\n"
            "- SELECT partyid FROM semantic_layer_dev_parties LIMIT 10\n"
        ),
        semantic_layer_id="semantic-rag-raw-dynamodb-x",
        semantic_layer_version="v1",
    )
    attrs = _sidecar_attrs(fake_s3)
    assert attrs["catalog_id"] == "dynamodb_catalog"
    assert attrs["database_name"] == "default"
    assert attrs["table_name"] == "semantic-layer-dev-parties"
    # The markdown body's table references were rewritten to the QUOTED connector
    # name (hyphenated → invalid as a bare SQL identifier) so generated FROM works.
    body = _doc_body(fake_s3)
    assert '"semantic-layer-dev-parties"' in body
    assert "FROM semantic_layer_dev_parties" not in body  # old bare Glue name gone from SQL


def test_sidecar_unchanged_for_non_ddb_table(monkeypatch):
    fake_s3 = _capture_s3(monkeypatch, "s3://bucket/warehouse/party/")
    main.save_metadata_document_to_s3(
        database_name="normalized",
        table_name="party",
        catalog_id="s3tablescatalog/x",
        metadata_content="# normalized.party\n\n## Overview\nx\n",
        semantic_layer_id="layer-1",
        semantic_layer_version="v1",
    )
    attrs = _sidecar_attrs(fake_s3)
    assert attrs["database_name"] == "normalized"
    assert attrs["table_name"] == "party"
    assert attrs["catalog_id"] == "s3tablescatalog/x"
