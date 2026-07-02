"""Unit tests for the correction-language heuristic (Monitoring tab signal).

The detector must FIRE on unambiguous corrections (the production signal we
report) and stay QUIET on ordinary questions (a false positive inflates the
correction rate and erodes trust in the dashboard).
"""

from __future__ import annotations

import os
import sys

# Make the rest-api package importable.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

import pytest  # noqa: E402

from services.correction_language import (  # noqa: E402
    is_correction,
    matched_phrases,
)


@pytest.mark.parametrize(
    "text",
    [
        "That's the wrong table, use coverage instead",
        "you're missing the fraud filter",
        "You are missing the WHERE clause for active policies",
        "I meant policies, not products",
        "that's not correct",
        "this isn't right",
        "the join is wrong",
        "you forgot to filter by status",
        "use coverage instead of the holding table",
        "that column is incorrect",
        "not the products table — the policy_product one",
        # Recall cases the demonstrative/restatement/you-frame anchors must catch
        # (added after a review found the earlier rewrite dropped these).
        "I asked for active policies not all of them",
        "that is wrong",
        "you used the wrong join",
        "you picked the wrong one",
        "that is not the data I need",
        "no that is not what I asked",
        "wrong, I wanted premiums",
    ],
)
def test_detects_corrections(text):
    """Each unambiguous correction phrasing is flagged."""
    assert is_correction(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "How many active policies are there?",
        "Show me total premium by product line",
        "What tables can I ask about?",
        "List the top 10 customers by revenue",
        "Can you break this down by month?",
        "",
        "   ",
        # ADVERSARIAL-but-normal: phrasings that the earlier loose patterns
        # (bare "instead", "should be", "I meant", unanchored "not the")
        # wrongly flagged. The headline correction rate must NOT count these.
        "Show me revenue instead of cost",
        "I would use the metric table instead of computing",
        "Which table should be used for fraud?",
        "I meant to ask about premiums",
        "Can you tell me which is not the right table to join?",
        "use the normalized schema instead of raw view",
        # FILTER contrastives — "X, not Y" describing a projection/filter, NOT a
        # correction. A review found a bare ",not" pattern wrongly flagged these
        # (they name no correcting subject), reopening the inflation the rewrite
        # set out to fix. The schema-noun ones guard against a too-greedy fix.
        "show revenue, not including tax",
        "list active, not cancelled, policies",
        "filter to paid, not pending",
        "group by month, not by year",
        "I want totals, not averages",
        "group the table by month, not by year",
        "filter the column to paid, not pending",
    ],
)
def test_ignores_ordinary_questions(text):
    """Normal questions (incl. adversarial-but-normal) are NOT flagged."""
    assert is_correction(text) is False


def test_known_recall_limit_documented():
    """A contrastive that names the replacement only AFTER the comma without a
    correcting subject ("should be the party table, not holding") is a KNOWN
    accepted miss — it is indistinguishable from a filter contrastive. Pinned so
    a future change that 'fixes' it is forced to also prove it doesn't reopen the
    filter false-positive class (see test_ignores_ordinary_questions)."""
    assert is_correction("should be the party table, not holding") is False


def test_non_string_input_is_false():
    """Non-string input fails closed rather than raising."""
    assert is_correction(None) is False  # type: ignore[arg-type]
    assert is_correction(123) is False  # type: ignore[arg-type]


def test_matched_phrases_returns_distinct_snippets():
    """matched_phrases surfaces the concrete substrings that triggered a flag."""
    phrases = matched_phrases("That's the wrong table and you're missing the filter")
    assert phrases  # non-empty
    # distinct + lower-cased de-dupe
    assert len(phrases) == len({p.lower() for p in phrases})
    joined = " ".join(phrases).lower()
    assert "wrong table" in joined
    assert "missing the" in joined


def test_matched_phrases_truncates():
    """Each returned snippet respects max_len."""
    long_text = "you forgot " + ("x" * 500)
    phrases = matched_phrases(long_text, max_len=20)
    assert all(len(p) <= 20 for p in phrases)


def test_matched_phrases_empty_for_clean_text():
    """No snippets for a non-correction."""
    assert matched_phrases("How many policies are active?") == []
