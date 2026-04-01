"""Tests for GuardrailService."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

# Add lambda/rest-api to sys.path so 'services.guardrail_service' is importable
_REST_API_DIR = str(Path(__file__).resolve().parents[2] / 'lambda' / 'rest-api')
if _REST_API_DIR not in sys.path:
    sys.path.insert(0, _REST_API_DIR)


@pytest.fixture
def guardrail_env(monkeypatch):
    monkeypatch.setenv('GUARDRAIL_IDENTIFIER', 'test-guardrail-id')
    monkeypatch.setenv('GUARDRAIL_VERSION', '1')
    monkeypatch.setenv('AWS_REGION', 'us-east-1')


def test_apply_returns_not_blocked_when_no_intervention(guardrail_env):
    with patch('services.guardrail_service.boto3') as mock_boto:
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            'action': 'NONE', 'outputs': [], 'assessments': [{}]
        }
        mock_boto.client.return_value = mock_client

        from services.guardrail_service import GuardrailService
        svc = GuardrailService()
        result = svc.apply('What are the active policies?', source='INPUT')

    assert result['blocked'] is False
    assert result['action'] == 'NONE'
    assert result['message'] == ''


def test_apply_returns_blocked_when_guardrail_intervenes(guardrail_env):
    with patch('services.guardrail_service.boto3') as mock_boto:
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            'action': 'GUARDRAIL_INTERVENED',
            'outputs': [{'text': 'Content blocked.'}],
            'assessments': [{}],
        }
        mock_boto.client.return_value = mock_client

        from services.guardrail_service import GuardrailService
        svc = GuardrailService()
        result = svc.apply('some toxic input', source='INPUT')

    assert result['blocked'] is True
    assert result['message'] == 'Content blocked.'
    assert result['action'] == 'GUARDRAIL_INTERVENED'


def test_apply_blocked_with_empty_outputs_uses_default_message(guardrail_env):
    with patch('services.guardrail_service.boto3') as mock_boto:
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            'action': 'GUARDRAIL_INTERVENED',
            'outputs': [],
            'assessments': [{}],
        }
        mock_boto.client.return_value = mock_client

        from services.guardrail_service import GuardrailService
        svc = GuardrailService()
        result = svc.apply('bad input', source='INPUT')

    assert result['blocked'] is True
    assert result['message'] == 'Content blocked by safety policy.'


def test_apply_fails_open_on_api_error(guardrail_env):
    with patch('services.guardrail_service.boto3') as mock_boto:
        mock_client = MagicMock()
        mock_client.apply_guardrail.side_effect = Exception('service unavailable')
        mock_boto.client.return_value = mock_client

        from services.guardrail_service import GuardrailService
        svc = GuardrailService()
        result = svc.apply('hello', source='INPUT')

    assert result['blocked'] is False
    assert result['action'] == 'ERROR'


def test_apply_skips_when_not_configured(monkeypatch):
    monkeypatch.setenv('GUARDRAIL_IDENTIFIER', '')
    monkeypatch.setenv('GUARDRAIL_VERSION', '')

    from services.guardrail_service import GuardrailService
    svc = GuardrailService()

    assert svc.enabled is False
    result = svc.apply('anything', source='INPUT')
    assert result['blocked'] is False
    assert result['action'] == 'NONE'


def test_apply_uses_output_source_for_agent_responses(guardrail_env):
    with patch('services.guardrail_service.boto3') as mock_boto:
        mock_client = MagicMock()
        mock_client.apply_guardrail.return_value = {
            'action': 'NONE', 'outputs': [], 'assessments': [{}]
        }
        mock_boto.client.return_value = mock_client

        from services.guardrail_service import GuardrailService
        svc = GuardrailService()
        svc.apply('agent response text', source='OUTPUT')

    mock_client.apply_guardrail.assert_called_once_with(
        guardrailIdentifier='test-guardrail-id',
        guardrailVersion='1',
        source='OUTPUT',
        content=[{'text': {'text': 'agent response text'}}],
    )
