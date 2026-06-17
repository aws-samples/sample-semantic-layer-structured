"""Unit tests for the KB metadata sidecar written by
``save_metadata_document_to_s3``.

Bedrock KB enforces a hard 1,024-byte limit on the companion ``.metadata.json``
sidecar and SILENTLY drops the whole document at ingestion when it's exceeded
(surfaced only as a job-level "Ignored N files …" failureReason). Wide core
tables (party, rider, policy_product, coverage …) used to breach this because
the sidecar carried bulky derived keys (column_names, referenced_tables,
join_keys, acord_path), so they never got indexed and the query agent couldn't
find them. These tests pin the fix: the sidecar carries ONLY the small
filter/structural keys, stays well under the limit even for a huge document,
and fails loudly (never silently) if a pathological identifier breaches it.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.metadata_agent import main
from agents.metadata_agent.main import (
    _KB_METADATA_MAX_BYTES,
    save_metadata_document_to_s3,
)


def _capture_s3(monkeypatch) -> MagicMock:
    """Stub the boto3 S3 client + bucket env so put_object calls are captured."""
    fake_s3 = MagicMock()
    fake_session = MagicMock()
    fake_session.client.return_value = fake_s3
    monkeypatch.setattr(main, "get_boto_session", lambda: fake_session)
    monkeypatch.setenv("ARTIFACTS_BUCKET", "test-bucket")
    return fake_s3


def _sidecar_body(fake_s3: MagicMock) -> bytes:
    """Return the Body bytes of the .metadata.json put_object call."""
    for call in fake_s3.put_object.call_args_list:
        if call.kwargs["Key"].endswith(".metadata.json"):
            return call.kwargs["Body"]
    raise AssertionError("no .metadata.json put_object call captured")


# A deliberately HUGE markdown doc — the kind of wide, heavily-related table
# (party/rider/policy_product) whose old sidecar blew past 1024 bytes.
def _huge_doc() -> str:
    cols = "\n".join(
        f"| col_{i} | varchar | Description of column number {i} blah blah. |"
        for i in range(120)
    )
    refs = "\n".join(
        f"- `ref_table_{i}`: JOIN ref_table_{i} r ON t.key_{i} = r.key_{i}"
        for i in range(40)
    )
    return (
        "# s3tablescatalog.normalized.party\n\n## Overview\nOne row per party.\n\n"
        "## ACORD Source Path\nOLifE/Party (Party_Type). Maps to many elements.\n\n"
        f"## Reference Tables\n{refs}\n\n## Columns\n| Column | Type | Description |\n"
        f"|--------|------|-------------|\n{cols}\n"
    )


def test_sidecar_stays_under_limit_for_huge_doc(monkeypatch):
    fake_s3 = _capture_s3(monkeypatch)

    result = json.loads(
        save_metadata_document_to_s3(
            database_name="normalized",
            table_name="party",
            catalog_id="s3tablescatalog/semantic-layer-dev-analytics-tables",
            metadata_content=_huge_doc(),
            semantic_layer_id="semantic-rag-multi_table_curated_layer-6d39d755",
            semantic_layer_version="v1",
        )
    )

    assert result["success"] is True
    body = _sidecar_body(fake_s3)
    assert len(body) <= _KB_METADATA_MAX_BYTES, (
        f"sidecar is {len(body)} bytes — would be silently dropped by Bedrock KB"
    )


def test_sidecar_carries_only_filter_and_structural_keys(monkeypatch):
    fake_s3 = _capture_s3(monkeypatch)

    save_metadata_document_to_s3(
        database_name="normalized",
        table_name="party",
        catalog_id="cat",
        metadata_content=_huge_doc(),
        semantic_layer_id="layer-1",
        semantic_layer_version="v1",
    )

    attrs = json.loads(_sidecar_body(fake_s3))["metadataAttributes"]
    assert set(attrs) == {
        "semantic_layer_id",
        "semantic_layer_version",
        "database_name",
        "table_name",
        "catalog_id",
    }
    # The bulky derived keys must NOT be persisted — they bloated the sidecar and
    # are never used as KB filters (the query agent re-parses them from the body).
    for dropped in ("column_names", "referenced_tables", "join_keys", "acord_path"):
        assert dropped not in attrs


def test_oversized_identifier_fails_loudly(monkeypatch):
    """A pathological identifier that breaches the limit must surface as an
    error (returned in the tool's JSON), never a silent KB drop."""
    fake_s3 = _capture_s3(monkeypatch)

    result = json.loads(
        save_metadata_document_to_s3(
            database_name="normalized",
            table_name="t" * 1100,  # alone exceeds the 1024-byte sidecar budget
            catalog_id="cat",
            metadata_content="# t\n\n## Overview\nx",
            semantic_layer_id="layer-1",
            semantic_layer_version="v1",
        )
    )

    assert result["success"] is False
    assert "1024" in result["error"] or "limit" in result["error"].lower()
    # The oversized sidecar must NOT have been written.
    assert not any(
        c.kwargs["Key"].endswith(".metadata.json")
        for c in fake_s3.put_object.call_args_list
    )
