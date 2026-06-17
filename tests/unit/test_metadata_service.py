"""
Unit Tests for MetadataService

Tests the metadata enrichment and query status methods.
"""

import pytest
import sys
import os

# Add lambda/rest-api directory to sys.path so we can import services
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))


def test_start_metadata_enrichment_returns_202_payload():
    """Test that start_metadata_enrichment returns correct payload structure"""
    from unittest.mock import MagicMock, patch

    with patch('services.metadata_service.boto3') as mock_boto, \
         patch('services.metadata_service.AgentCoreService') as mock_ac:
        mock_ac_inst = MagicMock()
        mock_ac.return_value = mock_ac_inst
        mock_ac_inst.invoke_metadata_agent.return_value = {'success': True}

        mock_table = MagicMock()
        test_id = 'test-id-xxxx-1234-5678-abcd'
        mock_table.query.return_value = {'Items': [{
            'id': test_id, 'version': 'v1',
            'dataSources': [{'databaseName': 'mydb', 'tableName': 'orders', 'catalogId': 'AWSDataCatalog'}],
        }]}
        mock_boto.resource.return_value.Table.return_value = mock_table

        from services.metadata_service import MetadataService
        svc = MetadataService()
        result = svc.start_metadata_enrichment(id=test_id)

        assert result['jobId'] == test_id
        assert result['status'] == 'processing'


def test_get_enrichment_status_reads_correct_key():
    """Test that get_enrichment_status reads from DynamoDB with correct key"""
    from unittest.mock import MagicMock, patch

    with patch('services.metadata_service.boto3') as mock_boto, \
         patch('services.metadata_service.AgentCoreService'):
        mock_table = MagicMock()
        test_id = 'job-1'
        mock_table.query.return_value = {
            'Items': [{
                'id': test_id,
                'version': 'v1',
                'status': 'processing',
                'tablesProcessed': 2,
                'totalTables': 5,
                'progressPercent': 40
            }]
        }

        mock_boto.resource.return_value.Table.return_value = mock_table

        from services.metadata_service import MetadataService
        svc = MetadataService()
        result = svc.get_enrichment_status(test_id)

        # Verify response contains status and progress
        assert result['status'] == 'processing'
        assert result['progressPercent'] == 40


def test_start_enrichment_passes_annotations_to_agentcore():
    """Annotations are stored in DynamoDB and invoke_metadata_agent is called with job_id only."""
    from unittest.mock import MagicMock, patch

    with patch('services.metadata_service.boto3') as mock_boto, \
         patch('services.metadata_service.AgentCoreService') as mock_ac:
        mock_ac_inst = MagicMock()
        mock_ac.return_value = mock_ac_inst
        mock_ac_inst.invoke_metadata_agent.return_value = {'success': True}

        mock_table = MagicMock()
        test_id = 'test-id-xxxx-1234-5678-abcd'
        mock_table.query.return_value = {'Items': [{
            'id': test_id, 'version': 'v1',
            'dataSources': [{'databaseName': 'mydb', 'tableName': 'orders', 'catalogId': 'AWSDataCatalog'}],
        }]}
        mock_table.update_item.return_value = {}
        mock_boto.resource.return_value.Table.return_value = mock_table

        from services.metadata_service import MetadataService
        svc = MetadataService()

        annotations = [{'target': 'table_description', 'instruction': 'monthly snapshot'}]
        result = svc.start_metadata_enrichment(
            id=test_id,
            annotations=annotations,
        )
        assert result['status'] == 'processing'

        # Annotations must be stored in DynamoDB via update_item
        update_call = mock_table.update_item.call_args
        expr_values = update_call.kwargs.get('ExpressionAttributeValues', {})
        assert expr_values.get(':annotations') == annotations

        # invoke_metadata_agent is called with id only — no tables or annotations
        mock_ac_inst.invoke_metadata_agent.assert_called_once_with(id=test_id)


def test_start_enrichment_filters_to_target_tables():
    """When target_tables is set, totalTables reflects the filter; agent called with job_id only."""
    from unittest.mock import MagicMock, patch

    with patch('services.metadata_service.boto3') as mock_boto, \
         patch('services.metadata_service.AgentCoreService') as mock_ac:
        mock_ac_inst = MagicMock()
        mock_ac.return_value = mock_ac_inst
        mock_ac_inst.invoke_metadata_agent.return_value = {'success': True}

        mock_table = MagicMock()
        test_id = 'test-id-xxxx-1234-5678-abcd'
        mock_table.query.return_value = {'Items': [{
            'id': test_id, 'version': 'v1',
            'dataSources': [
                {'databaseName': 'mydb', 'tableName': 'orders', 'catalogId': 'AWSDataCatalog'},
                {'databaseName': 'mydb', 'tableName': 'customers', 'catalogId': 'AWSDataCatalog'},
            ],
        }]}
        mock_table.update_item.return_value = {}
        mock_boto.resource.return_value.Table.return_value = mock_table

        from services.metadata_service import MetadataService
        svc = MetadataService()

        svc.start_metadata_enrichment(
            id=test_id,
            target_tables=['mydb.orders'],
        )

        # totalTables in DynamoDB update_item reflects the filtered count (1, not 2)
        update_call = mock_table.update_item.call_args
        expr_values = update_call.kwargs.get('ExpressionAttributeValues', {})
        assert expr_values.get(':total') == 1

        # invoke_metadata_agent is called with id only
        mock_ac_inst.invoke_metadata_agent.assert_called_once_with(id=test_id)


def test_update_table_kb_metadata_does_not_exist():
    """update_table_kb_metadata must be removed."""
    from unittest.mock import patch
    with patch('services.metadata_service.boto3'), \
         patch('services.metadata_service.AgentCoreService'):
        from services.metadata_service import MetadataService
        svc = MetadataService()
        assert not hasattr(svc, 'update_table_kb_metadata'), \
            "update_table_kb_metadata should have been removed"


def test_get_metadata_versions_returns_sorted_list():
    """get_metadata_versions returns all version records sorted newest-first."""
    from unittest.mock import MagicMock, patch

    with patch('services.metadata_service.boto3') as mock_boto, \
         patch('services.metadata_service.AgentCoreService'):
        mock_table = MagicMock()
        mock_table.query.return_value = {
            'Items': [
                {'id': 'abc', 'version': 'v1', 'status': 'completed', 'updatedAt': 't1'},
                {'id': 'abc', 'version': 'v2', 'status': 'completed', 'updatedAt': 't2'},
            ]
        }
        mock_boto.resource.return_value.Table.return_value = mock_table

        from services.metadata_service import MetadataService
        svc = MetadataService()
        versions = svc.get_metadata_versions('abc')

    assert len(versions) == 2
    assert versions[0]['version'] == 'v2'   # newest first
    assert versions[1]['version'] == 'v1'
    assert 'status' in versions[0]
    assert 'updatedAt' in versions[0]


def test_start_metadata_revision_stamps_active_version_and_invokes_agent():
    """start_metadata_revision sets revisionMode=True on the active (highest) version
    and calls invoke_metadata_agent."""
    from unittest.mock import MagicMock, patch, call

    with patch('services.metadata_service.boto3') as mock_boto, \
         patch('services.metadata_service.AgentCoreService') as mock_ac:
        mock_ac_inst = MagicMock()
        mock_ac.return_value = mock_ac_inst

        mock_table = MagicMock()
        test_id = 'test-id-xxxx-1234-5678-abcd'
        v1_record = {
            'id': test_id, 'version': 'v1', 'status': 'inactive',
            'dataSources': [{'databaseName': 'db', 'tableName': 'tbl'}],
        }
        v2_record = {
            'id': test_id, 'version': 'v2', 'status': 'completed',
            'dataSources': [{'databaseName': 'db', 'tableName': 'tbl'}],
        }
        # query returns both v1 and v2 → active is v2, next should be v3
        mock_table.query.return_value = {
            'Items': [dict(v1_record), dict(v2_record)]
        }
        mock_boto.resource.return_value.Table.return_value = mock_table

        from services.metadata_service import MetadataService
        svc = MetadataService()
        result = svc.start_metadata_revision(
            id=test_id,
            base_version='v2',
            annotations=[{'highlightedText': 'x', 'comment': 'fix y'}],
        )

    # put_item stamps the active version (v2), not v1
    put_call_args = mock_table.put_item.call_args[1]['Item']
    assert put_call_args['revisionMode'] is True
    assert put_call_args['targetVersion'] == 'v3'
    assert put_call_args['version'] == 'v2'
    assert put_call_args['revisionInstructions'] == [{'highlightedText': 'x', 'comment': 'fix y'}]

    # agent was invoked
    mock_ac_inst.invoke_metadata_agent.assert_called_once_with(id=test_id)

    # return shape
    assert result['status'] == 'building'
    assert result['nextVersion'] == 'v3'


def test_parse_metadata_markdown_extracts_all_sections():
    """_parse_metadata_markdown must return EVERY ## section (not just Overview
    + Columns) so the Metadata tab can render the full curated KB document."""
    from unittest.mock import patch, MagicMock
    with patch('services.metadata_service.boto3'), \
         patch('services.metadata_service.AgentCoreService'):
        from services.metadata_service import MetadataService
        svc = MetadataService()

    md = (
        "# cat.db.address\n\n"
        "## Overview\nPostal address of a party.\n\n"
        "## Business Purpose\nLocate where a customer lives.\n\n"
        "## Reference Tables\n- `party`: JOIN party p ON address.party_id = p.party_id\n\n"
        "## Columns\n"
        "| Column | Type | Description |\n"
        "|--------|------|-------------|\n"
        "| party_id | string | FK to party. |\n"
        "| city | string | City of the address. |\n\n"
        "## Notes\nAudit-only soft delete.\n"
    )
    parsed = svc._parse_metadata_markdown(md)

    # Overview still drives the short description; columns still parse.
    assert parsed['description'] == 'Postal address of a party.'
    assert [c['name'] for c in parsed['columns']] == ['party_id', 'city']
    assert parsed['columns'][0]['description'] == 'FK to party.'

    # NEW: every section is captured in document order for full-doc rendering.
    titles = [s['title'] for s in parsed['sections']]
    assert titles == [
        'Overview', 'Business Purpose', 'Reference Tables', 'Columns', 'Notes',
    ]
    bp = next(s for s in parsed['sections'] if s['title'] == 'Business Purpose')
    assert bp['body'] == 'Locate where a customer lives.'
    ref = next(s for s in parsed['sections'] if s['title'] == 'Reference Tables')
    assert 'JOIN party p' in ref['body']
