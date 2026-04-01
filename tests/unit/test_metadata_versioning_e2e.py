"""
Smoke test: full metadata versioning flow (service + agent side, all mocked).

Run:
    cd /Users/huthmac/Documents/AWS/00_workspace/semantic-layer
    pytest tests/unit/test_metadata_versioning_e2e.py -v
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))


def test_full_revision_round_trip():
    """
    Simulates the highest-version-as-active-record model:
    - service.start_metadata_revision() reads the active (highest) version and
      stamps it with revisionMode + targetVersion.
    - agent._write_versioned_completion() writes the new version record via
      put_item and marks the previous version inactive via update_item.
    """
    test_id = 'smoke-test-id-abcd-1234'
    put_items = []

    # --- Service side ---
    with patch('services.metadata_service.boto3') as mock_boto, \
         patch('services.metadata_service.AgentCoreService') as mock_ac:
        mock_ac_inst = MagicMock()
        mock_ac.return_value = mock_ac_inst

        mock_table = MagicMock()
        v1_record = {
            'id': test_id, 'version': 'v1', 'status': 'completed',
            'dataSources': [{'databaseName': 'db', 'tableName': 'tbl'}],
        }
        # Both query calls (get_metadata_versions + _get_latest_metadata_item) return v1
        mock_table.query.return_value = {'Items': [dict(v1_record)]}
        mock_table.put_item.side_effect = lambda Item: put_items.append(dict(Item))
        mock_boto.resource.return_value.Table.return_value = mock_table

        from services.metadata_service import MetadataService
        svc = MetadataService()
        result = svc.start_metadata_revision(
            id=test_id, base_version='v1',
            annotations=[{'comment': 'add better descriptions'}]
        )

    assert result['nextVersion'] == 'v2'
    stamped = put_items[-1]
    assert stamped['revisionMode'] is True
    assert stamped['targetVersion'] == 'v2'
    put_items.clear()

    # --- Agent side ---
    config = {**stamped}
    with patch('metadata_agent.main.get_boto_session') as mock_sess:
        agent_table = MagicMock()
        agent_table.put_item.side_effect = lambda Item: put_items.append(dict(Item))
        mock_sess.return_value.resource.return_value.Table.return_value = agent_table

        from metadata_agent.main import _write_versioned_completion
        _write_versioned_completion(test_id, config, 'v2', 'Processed 1/1 tables.')

    # Only one put_item: the new active version record
    assert len(put_items) == 1
    new_record = put_items[0]
    assert new_record['version'] == 'v2'
    assert new_record['status'] == 'completed'
    assert new_record['revisionMode'] is False
    assert 'revisionInstructions' not in new_record
    assert 'targetVersion' not in new_record
    assert 'currentVersion' not in new_record

    # One update_item: marks the previous version (v1) as inactive
    assert agent_table.update_item.call_count == 1
    update_kwargs = agent_table.update_item.call_args[1]
    assert update_kwargs['Key'] == {'id': test_id, 'version': 'v1'}
    assert update_kwargs['ExpressionAttributeValues'][':inactive'] == 'inactive'
