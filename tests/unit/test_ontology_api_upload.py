"""Tests for the /upload endpoint validation in ontology_api.py."""
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))


@pytest.fixture
def client():
    with patch('services.ontology_service.OntologyService.__init__', return_value=None):
        # Import fresh each time — patch must be active before import
        import importlib
        import ontology_api
        importlib.reload(ontology_api)
        from fastapi.testclient import TestClient
        return TestClient(ontology_api.app)


def test_upload_unsupported_type_returns_400(client):
    response = client.post(
        '/upload',
        data={'id': 'test-123'},
        files={'file': ('resume.doc', b'\xd0\xcf\x11\xe0', 'application/octet-stream')},
    )
    assert response.status_code == 400
    assert 'Unsupported file type' in response.json()['detail']


def test_upload_supported_type_calls_service(client):
    mock_result = {
        'id': 'test-123',
        'filename': 'notes.txt',
        'path': 's3://b/k/notes.txt',
        'status': 'uploaded',
    }
    with patch('ontology_api.ontology_service.upload_metadata_file', return_value=mock_result):
        response = client.post(
            '/upload',
            data={'id': 'test-123'},
            files={'file': ('notes.txt', b'hello', 'text/plain')},
        )
    assert response.status_code == 200
    assert response.json()['status'] == 'uploaded'
