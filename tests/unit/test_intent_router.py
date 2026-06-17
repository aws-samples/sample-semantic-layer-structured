"""Unit tests for the intent router (agents.shared.advisory.classify_intent).

Covers the regex fast-path (no model call), the model gray-zone with the
confidence floor, and the conservative default to data_query on low confidence,
no classifier, or a classifier error. The model classifier is injected as a
stub, so these tests need no Strands/Bedrock.
"""
import pytest

from agents.shared.advisory import classify_intent


def test_regex_fast_path_advisory_skips_model():
    """An obvious meta question is advisory via regex — classify_fn is never called."""
    called = {"n": 0}

    def classify_fn(q):
        called["n"] += 1
        return {"intent": "data_query", "confidence": 1.0}

    verdict = classify_intent(question="what metrics could I calculate?",
                              classify_fn=classify_fn)
    assert verdict["intent"] == "advisory"
    assert verdict["confidence"] == 1.0
    assert called["n"] == 0  # regex short-circuited before the model


def test_no_classifier_defaults_to_data_query():
    """With no classify_fn, a non-regex question defaults to data_query (today's path)."""
    verdict = classify_intent(question="how many parties are there?")
    assert verdict["intent"] == "data_query"


def test_model_advisory_above_floor_is_honored():
    """A confident advisory verdict from the model routes to advisory."""
    verdict = classify_intent(
        question="tell me about this dataset",
        classify_fn=lambda q: {"intent": "advisory", "confidence": 0.9},
    )
    assert verdict["intent"] == "advisory"


def test_model_advisory_below_floor_downgraded():
    """A low-confidence advisory verdict is downgraded to data_query (conservative)."""
    verdict = classify_intent(
        question="show me something interesting",
        classify_fn=lambda q: {"intent": "advisory", "confidence": 0.5},
    )
    assert verdict["intent"] == "data_query"


def test_model_data_query_passthrough():
    """A data_query verdict stays data_query regardless of confidence."""
    verdict = classify_intent(
        question="what is the total payout?",
        classify_fn=lambda q: {"intent": "data_query", "confidence": 0.95},
    )
    assert verdict["intent"] == "data_query"


def test_classifier_error_falls_back_to_data_query():
    """A classify_fn that raises must not propagate — default to data_query."""
    def boom(q):
        raise RuntimeError("model down")

    verdict = classify_intent(question="ambiguous thing", classify_fn=boom)
    assert verdict["intent"] == "data_query"
    assert verdict["confidence"] == 0.0


def test_metric_named_is_not_advisory():
    """A metric_named verdict is not pulled into the advisory route."""
    verdict = classify_intent(
        question="compute revenue_ttm",
        classify_fn=lambda q: {"intent": "metric_named", "confidence": 0.9},
    )
    assert verdict["intent"] != "advisory"
