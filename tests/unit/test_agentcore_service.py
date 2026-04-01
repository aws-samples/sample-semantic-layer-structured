"""Unit tests for AgentCoreService.invoke_metadata_agent."""
import sys, os, json
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))


def test_invoke_metadata_agent_payload_is_id_only():
    """invoke_metadata_agent sends only {"id": job_id} — no tables or annotations."""
    from unittest.mock import MagicMock, patch

    with patch('services.agentcore_service.boto3') as mock_boto:
        mock_session = MagicMock()
        mock_boto.Session.return_value = mock_session
        mock_session.get_credentials.return_value = MagicMock()

        mock_client = MagicMock()
        mock_boto.client.return_value = mock_client
        mock_client.invoke_agent_runtime.return_value = {
            'response': [json.dumps({'status': 'processing'}).encode()]
        }

        from services.agentcore_service import AgentCoreService
        svc = AgentCoreService()
        svc.metadata_runtime_arn = 'arn:aws:bedrock:us-east-1::agent-runtime/fake'

        job_id = 'a' * 36
        svc.invoke_metadata_agent(id=job_id)

        call_args = mock_client.invoke_agent_runtime.call_args
        payload = json.loads(call_args.kwargs['payload'])
        assert payload == {'id': job_id}
        assert 'tables' not in payload
        assert 'annotations' not in payload


def test_invoke_metadata_agent_raises_without_arn():
    """ValueError raised when METADATA_RUNTIME_ARN is not set."""
    from unittest.mock import MagicMock, patch

    with patch('services.agentcore_service.boto3') as mock_boto:
        mock_session = MagicMock()
        mock_boto.Session.return_value = mock_session
        mock_session.get_credentials.return_value = MagicMock()

        from services.agentcore_service import AgentCoreService
        svc = AgentCoreService()
        svc.metadata_runtime_arn = None  # not configured

        with pytest.raises(ValueError, match='METADATA_RUNTIME_ARN'):
            svc.invoke_metadata_agent(id='a' * 36)
