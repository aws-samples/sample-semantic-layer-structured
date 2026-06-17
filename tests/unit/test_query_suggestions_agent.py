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


def test_invoke_caps_suggestions_at_three():
    """If the agent overshoots and returns more than 3 suggestions, invoke()
    must truncate to 3 (server-side enforcement of the prompt's 'exactly 3')."""
    overshoot = json.dumps({
        'suggestions': [
            {'category': f'Cat {i}', 'question': f'Question {i}?'} for i in range(5)
        ]
    })
    mock_response = _make_agent_response(overshoot)

    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.create_suggestions_agent') as mock_create:
        mock_agent = MagicMock(return_value=mock_response)
        mock_create.return_value = mock_agent

        result = invoke({'id': 'test-id'}, context=None)

    assert len(result['suggestions']) == 3
    # First 3 preserved in order.
    assert [s['category'] for s in result['suggestions']] == ['Cat 0', 'Cat 1', 'Cat 2']


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


# ---------------------------------------------------------------------------
# Tests: advisory mode (free-form questions ABOUT the layer)
# ---------------------------------------------------------------------------

def test_invoke_advisory_mode_returns_answer_not_suggestions():
    """A payload with a question routes to advisory and returns an answer dict
    (with the structural no-SQL guarantee), not the 3-suggestion shape."""
    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.build_advisory_answer') as mock_advisory:
        mock_advisory.return_value = {
            'answer': 'You can ask about policies and coverage.',
            'metrics': [{'metric_id': 'm1', 'name': 'Revenue', 'description': 'TTM'}],
            'executed_sql': '',
            'results': [],
            'kb_empty': False,
        }
        result = invoke(
            {'id': 'test-id', 'question': 'what can I ask?'}, context=None,
        )

    assert 'suggestions' not in result
    assert result['answer'] == 'You can ask about policies and coverage.'
    assert result['executed_sql'] == ''
    assert result['results'] == []
    # The advisory builder was called with the layer id + question.
    _, kwargs = mock_advisory.call_args
    assert kwargs['layer_id'] == 'test-id'
    assert kwargs['question'] == 'what can I ask?'


def test_invoke_advisory_mode_explicit_flag():
    """mode='advisory' selects advisory even with an empty question."""
    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.build_advisory_answer') as mock_advisory:
        mock_advisory.return_value = {'answer': 'x', 'metrics': [],
                                      'executed_sql': '', 'results': [], 'kb_empty': True}
        result = invoke({'id': 'test-id', 'mode': 'advisory'}, context=None)

    assert mock_advisory.called
    assert 'answer' in result


def test_invoke_no_question_preserves_suggestions_default():
    """No question + no advisory mode → unchanged 3-suggestion behavior; the
    advisory builder is never called."""
    mock_response = _make_agent_response(VALID_SUGGESTIONS_JSON)
    with patch('agents.query_suggestions_agent.main.get_latest_metadata_item', return_value=VALID_CONFIG), \
         patch('agents.query_suggestions_agent.main.create_suggestions_agent') as mock_create, \
         patch('agents.query_suggestions_agent.main.build_advisory_answer') as mock_advisory:
        mock_create.return_value = MagicMock(return_value=mock_response)
        result = invoke({'id': 'test-id'}, context=None)

    assert 'suggestions' in result
    assert not mock_advisory.called


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
