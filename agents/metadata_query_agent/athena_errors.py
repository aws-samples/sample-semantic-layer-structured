"""Athena/federated-connector error classification for the execution tool.

Pure, dependency-free helpers so they unit-test without boto3/Strands. Used by
``main.execute_sql_query`` to decide whether a FAILED Athena query should be
retried (transient connector errors) or surfaced as a deterministic failure.

Two error classes the DynamoDB Athena connector raises that the LLM cannot fix by
rewriting SQL, so they are handled deterministically in the tool:

1. **Transient connector cold-start / throttle** — the connector Lambda is still
   initializing (``409 CodeArtifactUserPendingException`` / "Lambda is initializing
   your function") or is being throttled. The query failed for an infrastructure
   reason, not an SQL reason → retry with backoff.
2. **ProjectionExpression size** — ``SELECT *`` / ``COUNT(*)`` over a very wide
   DynamoDB table makes the connector project every attribute, exceeding
   DynamoDB's ProjectionExpression size limit. Retrying the identical SQL loops, so
   it is NOT transient; the fix is to project explicit columns (handled at
   generation time; here we classify it so the caller surfaces an actionable error
   instead of retrying).
"""
from __future__ import annotations

# Substrings (lower-cased match) that mark a RETRYABLE transient infrastructure
# failure — the connector/Athena is temporarily unavailable, not the SQL.
_TRANSIENT_MARKERS = (
    "codeartifactuserpendingexception",
    "lambda is initializing your function",
    "is initializing",
    "throttlingexception",
    "toomanyrequestsexception",
    "rate exceeded",
    "slow down",
    "503",
    "service unavailable",
)

# Substrings marking the ProjectionExpression-size error (NON-retryable; the same
# SQL would fail identically). The remedy is explicit-column projection.
_PROJECTION_MARKERS = (
    "invalid projectionexpression",
    "expression size has exceeded the maximum allowed size",
)


def is_transient_error(reason: str) -> bool:
    """True iff the Athena failure reason is a retryable transient connector error.

    Args:
        reason: The Athena ``StateChangeReason`` (or a boto exception message)
            from a FAILED query.

    Returns:
        True when ``reason`` matches a known transient marker (connector
        cold-start / throttle). A ProjectionExpression error is NEVER transient,
        even if some throttle substring coincidentally appears — projection wins.
    """
    if not reason:
        return False
    low = reason.lower()
    if is_projection_size_error(reason):
        return False
    return any(marker in low for marker in _TRANSIENT_MARKERS)


def is_projection_size_error(reason: str) -> bool:
    """True iff the failure is the DynamoDB ProjectionExpression-size error.

    Args:
        reason: The Athena ``StateChangeReason`` from a FAILED query.

    Returns:
        True when the reason names the ProjectionExpression size limit — the
        signature of ``SELECT *`` / ``COUNT(*)`` over a very wide DynamoDB table.
    """
    if not reason:
        return False
    low = reason.lower()
    return any(marker in low for marker in _PROJECTION_MARKERS)
