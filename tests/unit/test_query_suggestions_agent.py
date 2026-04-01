"""
Unit tests for query_suggestions_agent.main

Tests the invoke() entrypoint and loadSuggestedQuestions logic in isolation.
All AWS calls (DynamoDB, Bedrock KB) are mocked.
"""
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock BedrockAgentCoreApp BEFORE module import
# The decorator should pass through the function, not wrap it
class MockBedrockAgentCoreApp:
    def __init__(self, debug=False):
        """Initialize with optional debug flag"""
        pass

    def entrypoint(self, func):
        """Entrypoint decorator that just returns the function as-is"""
        return func

# Install mock before importing the module
sys.modules['bedrock_agentcore'] = MagicMock()
sys.modules['bedrock_agentcore'].BedrockAgentCoreApp = MockBedrockAgentCoreApp

# Now import the module under test
from agents.query_suggestions_agent.main import invoke  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent_response(text: str) -> MagicMock:
    """Build a mock Strands Agent response with the given text content."""
    mock_response = MagicMock()
    mock_response.message = {'content': [{'text': text}]}
    return mock_response


VALID_CONFIG = {'id': 'test-id', 'version': 'v1', 'name': 'Insurance Data'}

VALID_SUGGESTIONS_JSON = json.dumps({
    'suggestions': [
        {'category': 'Policy Analysis', 'question': 'How many active policies are there?'},
        {'category': 'Customer Insights', 'question': 'What is the average customer age?'},
    ]
})


# ---------------------------------------------------------------------------
# Tests: input validation
# ---------------------------------------------------------------------------

def test_invoke_missing_id_returns_error():
    """invoke() with empty payload should return an error dict immediately."""
    result = invoke({}, context=None)
    assert 'error' in result
    assert 'id' in result['error'].lower()


def test_invoke_empty_id_returns_error():
    """invoke() with id='' should return an error dict immediately."""
    result = invoke({'id': ''}, context=None)
    assert 'error' in result


def test_invoke_unknown_ontology_returns_error():
    """invoke() when DynamoDB returns no items should return an error dict."""
    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=None):
        result = invoke({'id': 'nonexistent-id'}, context=None)
    assert 'error' in result
    assert 'not found' in result['error'].lower()


# ---------------------------------------------------------------------------
# Tests: successful path
# ---------------------------------------------------------------------------

def test_invoke_returns_suggestions_on_success():
    """invoke() should return the parsed suggestions dict from the agent."""
    mock_response = _make_agent_response(VALID_SUGGESTIONS_JSON)

    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.create_suggestions_agent') as mock_create:
        mock_agent = MagicMock(return_value=mock_response)
        mock_create.return_value = mock_agent

        result = invoke({'id': 'test-id'}, context=None)

    assert 'suggestions' in result
    assert len(result['suggestions']) == 2
    assert result['suggestions'][0]['category'] == 'Policy Analysis'
    assert result['suggestions'][0]['question'].endswith('?')


def test_invoke_uses_ontology_name_in_user_input():
    """invoke() should include the ontology name in the prompt sent to the agent."""
    mock_response = _make_agent_response(VALID_SUGGESTIONS_JSON)
    captured_input = {}

    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.create_suggestions_agent') as mock_create:
        def capture_call(user_input):
            captured_input['input'] = user_input
            return mock_response

        mock_agent = MagicMock(side_effect=capture_call)
        mock_create.return_value = mock_agent

        invoke({'id': 'test-id'}, context=None)

    assert 'Insurance Data' in captured_input.get('input', '')


# ---------------------------------------------------------------------------
# Tests: response parsing / error handling
# ---------------------------------------------------------------------------

def test_invoke_strips_markdown_fences_correctly():
    """invoke() should strip markdown code fences that wrap the entire response."""
    fenced = "```json\n" + VALID_SUGGESTIONS_JSON + "\n```"
    mock_response = _make_agent_response(fenced)

    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.create_suggestions_agent') as mock_create:
        mock_agent = MagicMock(return_value=mock_response)
        mock_create.return_value = mock_agent

        result = invoke({'id': 'test-id'}, context=None)

    assert 'suggestions' in result
    assert len(result['suggestions']) == 2


def test_invoke_does_not_strip_backticks_inside_json():
    """invoke() should NOT mangle JSON that contains backticks inside string values."""
    json_with_backticks = json.dumps({
        'suggestions': [
            {'category': 'Test', 'question': 'What is `column_name`?'}
        ]
    })
    mock_response = _make_agent_response(json_with_backticks)

    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.create_suggestions_agent') as mock_create:
        mock_agent = MagicMock(return_value=mock_response)
        mock_create.return_value = mock_agent

        result = invoke({'id': 'test-id'}, context=None)

    assert 'suggestions' in result
    assert '`column_name`' in result['suggestions'][0]['question']


def test_invoke_handles_non_json_response_gracefully():
    """invoke() should return an error dict when the agent returns non-JSON text."""
    mock_response = _make_agent_response('This is plain text, not JSON.')

    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.create_suggestions_agent') as mock_create:
        mock_agent = MagicMock(return_value=mock_response)
        mock_create.return_value = mock_agent

        result = invoke({'id': 'test-id'}, context=None)

    assert 'error' in result
    assert 'raw' in result


def test_invoke_handles_agent_exception_gracefully():
    """invoke() should return an error dict when the agent raises an exception."""
    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.create_suggestions_agent') as mock_create:
        mock_agent = MagicMock(side_effect=RuntimeError('Bedrock throttled'))
        mock_create.return_value = mock_agent

        result = invoke({'id': 'test-id'}, context=None)

    assert 'error' in result
    assert 'Agent execution failed' in result['error']
