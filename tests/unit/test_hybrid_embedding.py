"""Unit tests for the hybrid query agent's copy-and-own Titan v2 embedding helper.

Mirrors ``tests/unit/test_shared_embedding.py`` against the vendored copy at
``agents.semantic_layer_query_agent.embedding``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agents.semantic_layer_query_agent import embedding


def test_embed_text_calls_titan_v2_and_returns_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper invokes Titan v2 with the question as inputText and unpacks
    the response's `embedding` array into a Python list of floats."""
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = {
        'body': MagicMock(read=lambda: b'{"embedding": [0.1, 0.2, 0.3]}'),
    }
    monkeypatch.setattr(embedding, '_bedrock_client', lambda: fake_client)

    vec = embedding.embed_text('monthly revenue')

    assert vec == [0.1, 0.2, 0.3]
    fake_client.invoke_model.assert_called_once()
    kwargs = fake_client.invoke_model.call_args.kwargs
    assert kwargs['modelId'] == 'amazon.titan-embed-text-v2:0'
    assert b'monthly revenue' in kwargs['body']


def test_embed_text_rejects_empty_input() -> None:
    """Empty / whitespace-only input must fail loudly."""
    with pytest.raises(ValueError, match='empty'):
        embedding.embed_text('')

    with pytest.raises(ValueError, match='empty'):
        embedding.embed_text('   ')
