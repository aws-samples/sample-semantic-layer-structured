"""Unit tests for metadata_api.py endpoint shapes."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))

def test_metadata_enrich_request_accepts_optional_fields():
    from metadata_api import MetadataEnrichRequest
    req = MetadataEnrichRequest(
        id='abc',
        targetTables=['db.table'],
        annotations=[{'target': 'table_description', 'instruction': 'hint'}],
    )
    assert req.targetTables == ['db.table']
    assert req.annotations[0]['target'] == 'table_description'

def test_metadata_enrich_request_minimal():
    from metadata_api import MetadataEnrichRequest
    req = MetadataEnrichRequest(id='abc')
    assert req.targetTables is None
    assert req.annotations is None

def test_metadata_update_request_does_not_exist():
    import metadata_api as m
    assert not hasattr(m, 'MetadataUpdateRequest'), \
        "MetadataUpdateRequest should have been removed"

def test_revise_endpoint_returns_202():
    """POST /revise/{id}/{version_id} calls start_metadata_revision and returns 202."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient

    with patch('services.metadata_service.MetadataService.start_metadata_revision') as mock_revision:
        mock_revision.return_value = {
            'id': 'abc', 'status': 'building',
            'currentVersion': 'v1', 'nextVersion': 'v2',
            'message': 'Revision started. New version v2 pending.',
        }
        import metadata_api
        client = TestClient(metadata_api.app)
        resp = client.post('/revise/abc/v1', json={
            'annotations': [{'highlightedText': 'x', 'comment': 'fix y'}]
        })

    assert resp.status_code == 202
    body = resp.json()
    assert body['status'] == 'building'
    assert body['nextVersion'] == 'v2'
