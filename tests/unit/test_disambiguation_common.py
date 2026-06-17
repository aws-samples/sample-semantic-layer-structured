"""Unit tests for the mode-agnostic disambiguation helpers shared by both
query agents (``agents/shared/disambiguation_common.py``):

  * ``inflection_variants`` — number-inflection used to match a query term to a
    snake_case table / PascalCase class name. The naive ``rstrip('s')`` it
    replaced turned "parties" into "partie" and silently missed the ``party``
    table (session 4c8a50c7 infinite clarification loop).
  * ``build_clarification_from_options`` — rebuild a clarification payload from
    the options the user was already shown, so a low-confidence re-ask keeps a
    stable option set instead of a fresh non-deterministic top-5.
"""
from __future__ import annotations

from agents.shared.disambiguation_common import (
    build_clarification_from_options,
    inflection_variants,
)


def test_inflection_ies_to_y_both_directions() -> None:
    # The case the loop hinged on: parties <-> party (rstrip gave "partie").
    assert "party" in inflection_variants("parties")
    assert "parties" in inflection_variants("party")
    assert "policy" in inflection_variants("policies")


def test_inflection_es_after_sibilant() -> None:
    # address <-> addresses (rstrip gave "addresse"); box <-> boxes.
    assert "address" in inflection_variants("addresses")
    assert "addresses" in inflection_variants("address")
    assert "box" in inflection_variants("boxes")


def test_inflection_plain_s() -> None:
    assert "table" in inflection_variants("tables")
    assert "tables" in inflection_variants("table")


def test_inflection_includes_original_and_handles_empty() -> None:
    assert "party" in inflection_variants("party")
    assert inflection_variants("") == set()
    # A word ending in a vowel+y pluralizes with -s, not -ies (no false "daies").
    assert "dayies" not in inflection_variants("day")


def test_build_clarification_from_options_passes_options_through() -> None:
    opts = [
        {"id": "party", "label": "party (database: normalized)"},
        {"id": "relation", "label": "relation (database: normalized)"},
    ]
    out = build_clarification_from_options(options=opts, terms=["parties"])
    assert out["needs_clarification"] is True
    assert [o["id"] for o in out["options"]] == ["party", "relation"]
    assert out["terms"] == ["parties"]
    assert "parties" in out["clarification_question"]


def test_build_clarification_from_options_dedups_and_skips_malformed() -> None:
    opts = [
        {"id": "party", "label": "party"},
        {"id": "party", "label": "party dup"},  # dup id dropped
        {"label": "no id"},                       # skipped
        "not a dict",                              # skipped
    ]
    out = build_clarification_from_options(options=opts, terms=[])
    assert [o["id"] for o in out["options"]] == ["party"]
    # No terms -> generic question, no crash.
    assert out["clarification_question"] == "Could you clarify your request?"
