"""Unit tests for ``agents.shared.embedding`` — the single Titan v2 embedding
helper shared by Tier 1 metric KNN lookup and Tier 2 Phase 1 topic router.

We must verify the helper pins the canonical model id and request shape,
because the index-build path and the query-time path live in different
modules and could otherwise drift.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..'),
)

from agents.shared import embedding  # noqa: E402


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
    """Empty / whitespace-only input must fail loudly — silently embedding
    "" would put a degenerate vector into the KNN index."""
    with pytest.raises(ValueError, match='empty'):
        embedding.embed_text('')

    with pytest.raises(ValueError, match='empty'):
        embedding.embed_text('   ')
