"""Tests for agents/shared/guardrails.py."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agents'))


@pytest.fixture
def gr_env(monkeypatch):
    monkeypatch.setenv('GUARDRAIL_IDENTIFIER', 'gid')
    monkeypatch.setenv('GUARDRAIL_VERSION', '1')
    monkeypatch.setenv('AWS_REGION', 'us-east-1')


def test_blocked_extracts_canned_message(gr_env):
    from shared.guardrails import GuardrailService
    svc = GuardrailService()
    client = MagicMock()
    client.apply_guardrail.return_value = {
        'action': 'GUARDRAIL_INTERVENED',
        'outputs': [{'text': 'Blocked by policy.'}],
    }
    with patch.object(svc, '_get_client', return_value=client):
        out = svc.apply('bad input', source='INPUT')
    assert out == {'blocked': True, 'message': 'Blocked by policy.', 'action': 'GUARDRAIL_INTERVENED'}


def test_fails_open_on_api_error(gr_env):
    from shared.guardrails import GuardrailService
    svc = GuardrailService()
    client = MagicMock()
    client.apply_guardrail.side_effect = RuntimeError('boom')
    with patch.object(svc, '_get_client', return_value=client):
        out = svc.apply('text', source='OUTPUT')
    assert out['blocked'] is False and out['action'] == 'ERROR'


def test_disabled_when_unconfigured(monkeypatch):
    monkeypatch.delenv('GUARDRAIL_IDENTIFIER', raising=False)
    monkeypatch.delenv('GUARDRAIL_VERSION', raising=False)
    from shared.guardrails import GuardrailService
    assert GuardrailService().apply('x')['action'] == 'NONE'
