"""Unit tests for the Athena/connector error classifier (athena_errors)."""
from __future__ import annotations

from agents.metadata_query_agent.athena_errors import (
    is_transient_error,
    is_projection_size_error,
)


def test_cold_start_is_transient():
    assert is_transient_error(
        "GENERIC_USER_ERROR: ... CodeArtifactUserPendingException: Lambda is "
        "initializing your function. It will be ready to invoke shortly. (409)"
    )


def test_throttling_is_transient():
    assert is_transient_error("ThrottlingException: Rate exceeded")
    assert is_transient_error("TooManyRequestsException")


def test_deterministic_errors_not_transient():
    assert not is_transient_error("SCHEMA_NOT_FOUND: Schema 'x' does not exist")
    assert not is_transient_error("COLUMN_NOT_FOUND: Column 'y' cannot be resolved")
    assert not is_transient_error("line 1:1: mismatched input")
    assert not is_transient_error("")


def test_projection_size_detected():
    assert is_projection_size_error(
        "Invalid ProjectionExpression: Expression size has exceeded the maximum "
        "allowed size"
    )
    assert is_projection_size_error("...expression size has exceeded the maximum allowed size...")


def test_projection_size_is_not_transient():
    # Even if some transient-looking token coincided, projection wins (non-retryable).
    reason = ("Invalid ProjectionExpression: Expression size has exceeded the "
              "maximum allowed size")
    assert is_projection_size_error(reason)
    assert not is_transient_error(reason)


def test_non_projection_not_flagged():
    assert not is_projection_size_error("SCHEMA_NOT_FOUND")
    assert not is_projection_size_error("")
