"""
Unit tests for update_progress' phantom-row guard.

DynamoDB's update_item CREATES the item when the key is absent, and
``_resolve_active_version`` falls back to 'v1' when no record exists at all. So a progress
write against a stale or already-deleted ontology id used to mint a nameless orphan stub —
a metadata row with a status and a percentage but no name, type or data sources. The
ConditionExpression makes the write conditional on the record being real.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))


class _FakeConditionalCheckFailed(Exception):
    """Stand-in for botocore's dynamically generated ConditionalCheckFailedException."""


def _dynamodb_mock(*, condition_fails: bool):
    """Build a boto3 DynamoDB *resource* mock whose update_item optionally fails the condition.

    Args:
        condition_fails: When True, update_item raises the ConditionalCheckFailedException
                         class reachable at resource.meta.client.exceptions — i.e. the item
                         does not exist. When False it succeeds.

    Returns:
        Tuple of (resource mock, table mock) so a test can assert on the update_item call.
    """
    table = MagicMock()
    table.query.return_value = {"Items": [{"version": "v3"}]}
    if condition_fails:
        table.update_item.side_effect = _FakeConditionalCheckFailed("does not exist")

    resource = MagicMock()
    resource.Table.return_value = table
    # update_progress catches resource.meta.client.exceptions.ConditionalCheckFailedException,
    # so the mock has to expose the same class the side effect raises.
    resource.meta.client.exceptions.ConditionalCheckFailedException = (
        _FakeConditionalCheckFailed
    )
    return resource, table


def test_update_progress_writes_conditionally_on_the_record_existing():
    from ontology_agent.main import update_progress

    resource, table = _dynamodb_mock(condition_fails=False)
    with patch("ontology_agent.main.get_boto_session") as mock_sess:
        mock_sess.return_value.resource.return_value = resource
        result = json.loads(
            update_progress(
                ontology_id="ont-123",
                tables_processed=2,
                total_tables=4,
                current_table="parties",
            )
        )

    assert result["success"] is True
    assert result["progressPercent"] == 50
    kwargs = table.update_item.call_args.kwargs
    # Without this the update silently doubles as an insert.
    assert kwargs["ConditionExpression"] == "attribute_exists(id)"
    # The active version is resolved from the table, not assumed to be v1.
    assert kwargs["Key"] == {"id": "ont-123", "version": "v3"}


def test_update_progress_creates_no_item_for_an_unknown_id():
    from ontology_agent.main import update_progress

    resource, table = _dynamodb_mock(condition_fails=True)
    with patch("ontology_agent.main.get_boto_session") as mock_sess:
        mock_sess.return_value.resource.return_value = resource
        result = json.loads(
            update_progress(
                ontology_id="ont-deleted",
                tables_processed=1,
                total_tables=3,
                current_table="parties",
            )
        )

    # Reported, not swallowed: the caller is an agent loop, and a silent success would have
    # it keep writing progress into the void for the rest of the build.
    assert result["success"] is False
    assert "ont-deleted" in result["error"]
    # Exactly one attempt, and no unconditional retry that would defeat the guard.
    assert table.update_item.call_count == 1
    assert table.put_item.call_count == 0


def test_update_progress_still_reports_other_dynamodb_errors():
    """A ConditionalCheckFailed must not become the catch-all for every DynamoDB failure."""
    from ontology_agent.main import update_progress

    resource, table = _dynamodb_mock(condition_fails=False)
    table.update_item.side_effect = RuntimeError("ProvisionedThroughputExceeded")
    with patch("ontology_agent.main.get_boto_session") as mock_sess:
        mock_sess.return_value.resource.return_value = resource
        result = json.loads(
            update_progress(
                ontology_id="ont-123",
                tables_processed=1,
                total_tables=2,
                current_table="parties",
            )
        )

    assert result["success"] is False
    assert "ProvisionedThroughputExceeded" in result["error"]
