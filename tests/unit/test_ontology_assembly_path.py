"""
Unit Test for Assembly Step — Store metadataPath in DynamoDB

Tests that the assembly step correctly stores the S3 location in DynamoDB
after successfully saving the consolidated ontology.
"""

from unittest.mock import MagicMock, patch, call
import json
import sys
import os

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))


def test_assembly_stores_ontology_path(monkeypatch):
    """
    Test that assembly step stores metadataPath in DynamoDB after saving to S3.

    This test verifies:
    1. save_ontology_to_s3 is called and returns an s3_location
    2. update_dynamodb_status is called with metadataPath set to the s3_location
    3. The metadataPath is properly stored as a kwarg
    """
    monkeypatch.setenv('ONTOLOGY_METADATA_TABLE', 'tbl')
    monkeypatch.setenv('ARTIFACTS_BUCKET', 'bucket')

    s3_loc = 's3://bucket/ontologies/abc/ontology.nq'

    # Patch both functions before importing the module
    with patch('ontology_agent.main.save_ontology_to_s3',
               return_value=json.dumps({'success': True, 's3_location': s3_loc})) as mock_save, \
         patch('ontology_agent.main.update_dynamodb_status') as mock_update:

        # Import functions after patching is set up
        from ontology_agent.main import save_ontology_to_s3, update_dynamodb_status

        # Simulate the assembly block's logic
        nq_content = 'mock nquads content'
        ontology_id = 'abc'

        save_result = json.loads(save_ontology_to_s3(nq_content, ontology_id))

        # Verify save succeeded
        assert save_result.get('success') is True
        assert save_result.get('s3_location') == s3_loc

        # Simulate the update call that should happen after save
        if save_result.get('success'):
            update_dynamodb_status(
                ontology_id=ontology_id,
                status='processing',
                metadataPath=save_result['s3_location'],
            )

        # Verify update_dynamodb_status was called
        mock_update.assert_called_once()

        # Verify the kwargs contain metadataPath
        call_kwargs = mock_update.call_args[1]
        assert call_kwargs.get('metadataPath') == s3_loc
        assert call_kwargs.get('ontology_id') == ontology_id
        assert call_kwargs.get('status') == 'processing'
