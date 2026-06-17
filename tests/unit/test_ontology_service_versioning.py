"""
Unit Tests for OntologyService Versioning Helpers

Tests the _version_num helper function that parses version strings like 'v1', 'v10'.
"""

import pytest
import sys
import os

# Add lambda/rest-api directory to sys.path so we can import services
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))


def test_version_num_v1():
    """Test parsing 'v1' returns 1"""
    from services.ontology_service import _version_num
    assert _version_num('v1') == 1


def test_version_num_v10():
    """Test parsing 'v10' returns 10"""
    from services.ontology_service import _version_num
    assert _version_num('v10') == 10


def test_version_num_unknown():
    """Test parsing 'unknown' returns 0"""
    from services.ontology_service import _version_num
    assert _version_num('unknown') == 0


def test_get_ontology_versions_sorted():
    """Test get_metadata_versions returns versions sorted by version number descending"""
    from unittest.mock import MagicMock
    from services.ontology_service import OntologyService

    svc = OntologyService.__new__(OntologyService)
    svc.table = MagicMock()
    svc.table.query.return_value = {
        'Items': [
            {'ontologyId': 'x', 'version': 'v1', 'status': 'completed', 'metadataPath': 's3://b/a.nq', 'updatedAt': '2026-01-01'},
            {'ontologyId': 'x', 'version': 'v3', 'status': 'completed', 'metadataPath': 's3://b/c.nq', 'updatedAt': '2026-03-01'},
            {'ontologyId': 'x', 'version': 'v2', 'status': 'completed', 'metadataPath': 's3://b/b.nq', 'updatedAt': '2026-02-01'},
        ]
    }
    result = svc.get_metadata_versions('x')
    assert [r['version'] for r in result] == ['v3', 'v2', 'v1']


def test_get_ontology_content_returns_nquads():
    """Test get_metadata_content returns N-QUADS content from S3"""
    from unittest.mock import MagicMock
    from services.ontology_service import OntologyService

    svc = OntologyService.__new__(OntologyService)
    svc.artifacts_bucket = 'my-bucket'
    svc.table = MagicMock()
    svc.table.get_item.return_value = {
        'Item': {'ontologyId': 'x', 'version': 'v1',
                 'metadataPath': 's3://my-bucket/ontologies/x/ontology.nq'}
    }
    svc.s3_client = MagicMock()
    nq_sample = '<http://ex> <http://rdf#type> <http://owl#Class> <http://g> .'
    svc.s3_client.get_object.return_value = {
        'Body': MagicMock(read=lambda: nq_sample.encode())
    }
    result = svc.get_metadata_content('x', 'v1')
    assert result['content'] == nq_sample
    assert result['version'] == 'v1'
    assert result['s3Path'].endswith('.nq')


def test_get_ontology_content_raises_when_no_path():
    """Test get_metadata_content raises ValueError when no metadata file"""
    from unittest.mock import MagicMock
    from services.ontology_service import OntologyService

    svc = OntologyService.__new__(OntologyService)
    svc.artifacts_bucket = 'my-bucket'
    svc.table = MagicMock()
    svc.table.get_item.return_value = {
        'Item': {'ontologyId': 'x', 'version': 'v1', 'metadataPath': None}
    }
    with pytest.raises(ValueError, match="No metadata file"):
        svc.get_metadata_content('x', 'v1')


def test_start_revision_stores_context_and_triggers_agent():
    """Test start_revision_async stores context in v1 and triggers agent"""
    from unittest.mock import MagicMock
    from services.ontology_service import OntologyService

    svc = OntologyService.__new__(OntologyService)
    svc.table = MagicMock()
    svc.agentcore_service = MagicMock()
    # get_metadata_versions returns v1 and v2
    svc.table.query.return_value = {
        'Items': [
            {'ontologyId': 'x', 'version': 'v1', 'status': 'completed', 'metadataPath': 's3://b/a.nq'},
            {'ontologyId': 'x', 'version': 'v2', 'status': 'completed', 'metadataPath': 's3://b/b.nq'},
        ]
    }
    # get_metadata_config (reads v1) returns a config
    svc.table.get_item.return_value = {
        'Item': {'ontologyId': 'x', 'version': 'v1', 'status': 'completed',
                 'metadataPath': 's3://b/b.nq', 'name': 'test'}
    }
    annotations = [{'highlightedText': 'PolicyHolder', 'comment': 'Add subclass'}]
    result = svc.start_revision_async('x', 'v2', annotations)
    assert result['nextVersion'] == 'v3'
    assert result['status'] == 'building'
    put_call_kwargs = svc.table.put_item.call_args[1]['Item']
    assert put_call_kwargs['revisionMode'] is True
    assert put_call_kwargs['targetVersion'] == 'v3'
    svc.agentcore_service.invoke_ontology_agent.assert_called_once_with(id='x')
