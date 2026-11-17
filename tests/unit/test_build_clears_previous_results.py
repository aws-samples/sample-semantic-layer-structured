"""Starting a build must clear the PREVIOUS run's outcome from the config record.

``get_build_status`` returns ``error`` unconditionally, and the progress fields
whenever they are present. Both build starters wrote ``status='pending'`` on top of
whatever the last attempt left behind, so a freshly-started build polled as "in
progress, and here is the previous run's failure and its table counts".

Observed on 2026-08-15: a Hybrid rebuild three minutes into its KB phase still
reported the prior run's VKG error verbatim — indistinguishable, from the UI's
point of view, from failing again immediately.

These tests pin that the outcome attributes are gone from the item that is written,
for both the metadata (VKG/SemanticRAG) and the Hybrid starter, and that a genuine
failure still records its error.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

# Add lambda/rest-api to sys.path so `services.*` imports resolve, matching
# tests/unit/test_ontology_service_versioning.py.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "rest-api"))

from services.ontology_service import OntologyService  # noqa: E402

# What a failed run leaves on the record. Every one of these is surfaced by
# get_build_status, so every one of them must be cleared.
STALE_RESULTS = {
    "status": "failed",
    "error": "vkg: 4 of 812 thin-VKG N-Quads lines would be REJECTED",
    "tablesProcessed": 6,
    "totalTables": 6,
    "currentTable": "cards",
    "progressPercent": 95,
}


def _service() -> OntologyService:
    """Build an OntologyService with its AWS seams replaced by mocks.

    Bypasses ``__init__`` (which creates boto3 clients) the way
    test_ontology_service_versioning.py does.

    Returns:
        OntologyService: An instance whose ``table`` and ``agentcore_service`` are
            MagicMocks.
    """
    svc = OntologyService.__new__(OntologyService)
    svc.table = MagicMock()
    svc.agentcore_service = MagicMock()
    svc.artifacts_bucket = "bkt"
    return svc


def _config(**overrides) -> dict:
    """A previously-failed layer config, plus any overrides."""
    config = {
        "id": "51e29707",
        "version": "v1",
        "type": "Hybrid",
        "dataSources": [{"databaseName": "db", "tableName": "cards"}],
        **STALE_RESULTS,
    }
    config.update(overrides)
    return config


def _written_item(svc: OntologyService) -> dict:
    """The Item from the FIRST put_item call — the one that marks the build pending."""
    return svc.table.put_item.call_args_list[0].kwargs["Item"]


def test_the_helper_drops_every_result_attribute():
    svc = _service()
    cleared = svc._clear_previous_build_results(_config())
    for attribute in OntologyService._BUILD_RESULT_ATTRIBUTES:
        assert attribute not in cleared, attribute
    # Configuration must survive — only the run's outcome is dropped.
    assert cleared["dataSources"]
    assert cleared["type"] == "Hybrid"


def test_the_helper_is_safe_on_a_never_built_layer():
    """A first build has none of these attributes; popping must not raise."""
    svc = _service()
    fresh = {"id": "new", "version": "v1", "dataSources": [{"tableName": "t"}]}
    assert svc._clear_previous_build_results(dict(fresh)) == fresh


def test_hybrid_build_writes_a_record_with_no_stale_error():
    svc = _service()
    svc.get_metadata_config = MagicMock(return_value=_config())

    result = svc.start_build_hybrid_async("51e29707")

    assert result["status"] == "pending"
    item = _written_item(svc)
    assert item["status"] == "pending"
    assert "error" not in item
    assert "progressPercent" not in item
    assert "currentTable" not in item
    assert item["buildStartedAt"]


def test_metadata_build_writes_a_record_with_no_stale_error():
    svc = _service()
    svc.get_metadata_config = MagicMock(return_value=_config(type="VKG"))

    svc.start_build_metadata_async("51e29707")

    item = _written_item(svc)
    assert item["status"] == "pending"
    for attribute in OntologyService._BUILD_RESULT_ATTRIBUTES:
        assert attribute not in item, attribute


def test_a_failed_invocation_still_records_its_own_error():
    """Clearing the old error must not swallow a NEW one."""
    svc = _service()
    svc.get_metadata_config = MagicMock(return_value=_config())
    svc.agentcore_service.invoke_hybrid_creation_agent.side_effect = RuntimeError(
        "runtime throttled"
    )

    with pytest.raises(ValueError, match="runtime throttled"):
        svc.start_build_hybrid_async("51e29707")

    # Second put_item is the failure write-back.
    failed = svc.table.put_item.call_args_list[-1].kwargs["Item"]
    assert failed["status"] == "failed"
    assert "runtime throttled" in failed["error"]
