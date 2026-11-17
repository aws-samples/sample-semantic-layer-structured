"""Descriptions reach the Hybrid graph, so the viewer stops showing em-dashes.

A completed Hybrid layer's Metadata tab rendered "—" in the Description column of
every one of its datatype properties. Nothing was broken in the build: the
documentation phase had written a good one-line description for every column into
the KB markdown, and the structural phase emitted no ``rdfs:comment`` at all, so
``OntologyEditor.jsx`` — which reads exactly that predicate for that cell — had
nothing to render.

These tests cover the three pieces that close the gap: parsing the descriptions
out of the KB markdown (``kb_descriptions``), reading the document back from S3
(``kb_builder.read_kb_document``), and the build wiring that joins them
(``main._harvest_kb_descriptions`` / ``main._make_build_vkg``). The fail-soft
behaviour is pinned deliberately: descriptions are additive, and a layer whose
graph grounds every query must not fail over missing prose.

All AWS access is exercised through the modules' injected seams — no boto3.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from agents.semantic_layer_creation_agent import kb_builder, kb_descriptions
from agents.semantic_layer_creation_agent import main as creation_main

# A KB document in the exact shape query_prompts.KB_DOCUMENT_FORMAT mandates,
# trimmed to the sections that matter here.
KB_DOC = """\
# cat.acme.legalities

## Overview
This table records the **legal status** of a card in a game format. The grain is
one row per (card, format) pair.

## Business Purpose
Used by rules teams and deck-building tools to answer legality questions.

## Columns
| Column | Type | Description |
|--------|------|-------------|
| id | bigint | Surrogate primary key for each legality record. |
| format | string | The play format the legality is evaluated in. |
| uuid | string | Foreign key linking to `cards.uuid`. |

## Notes
- A card can have zero or many rows here.
"""


# ─── kb_descriptions: parsing ────────────────────────────────────────────────


def test_overview_becomes_the_table_description():
    """The class description comes from ``## Overview``, flattened to one line."""
    parsed = kb_descriptions.parse_kb_document(KB_DOC)
    assert parsed["table"] == (
        "This table records the legal status of a card in a game format. "
        "The grain is one row per (card, format) pair."
    )


def test_every_column_row_yields_a_description():
    """Each ``## Columns`` row's third cell becomes that column's description."""
    columns = kb_descriptions.parse_kb_document(KB_DOC)["columns"]
    assert set(columns) == {"id", "format", "uuid"}
    assert columns["uuid"] == "Foreign key linking to cards.uuid."


def test_the_header_and_separator_rows_are_not_columns():
    """A "column" named `column` or `---` would emit a property-less comment."""
    columns = kb_descriptions.parse_kb_document(KB_DOC)["columns"]
    assert "column" not in columns and "--------" not in columns


def test_prose_sections_are_never_harvested():
    """Only Overview + Columns cross into the graph; the rest stays in the KB."""
    parsed = kb_descriptions.parse_kb_document(KB_DOC)
    blob = str(parsed)
    assert "deck-building tools" not in blob
    assert "zero or many rows" not in blob


def test_a_long_description_is_capped_at_a_sentence_boundary():
    """Descriptions ride along in the query agent's ontology slice, so cap them."""
    long_first = "A" * 380 + ". " + "B" * 200
    doc = (
        "## Columns\n"
        "| Column | Type | Description |\n"
        "|---|---|---|\n"
        f"| c | string | {long_first} |\n"
    )
    text = kb_descriptions.parse_kb_document(doc)["columns"]["c"]
    assert len(text) <= kb_descriptions.MAX_COLUMN_DESCRIPTION_CHARS
    assert text.endswith(".") and "B" not in text


def test_a_long_description_without_a_sentence_boundary_is_truncated():
    """No boundary to cut at still must not exceed the cap."""
    doc = (
        "## Columns\n"
        "| Column | Type | Description |\n"
        "|---|---|---|\n"
        f"| c | string | {'A' * 900} |\n"
    )
    text = kb_descriptions.parse_kb_document(doc)["columns"]["c"]
    assert len(text) <= kb_descriptions.MAX_COLUMN_DESCRIPTION_CHARS + 1
    assert text.endswith("…")


def test_column_names_are_lowercased_for_case_insensitive_lookup():
    """The composer does not reliably reproduce the catalog's column-name case."""
    doc = (
        "## Columns\n"
        "| Column | Type | Description |\n"
        "|---|---|---|\n"
        "| `setCode` | string | The set's code. |\n"
    )
    assert kb_descriptions.parse_kb_document(doc)["columns"] == {
        "setcode": "The set's code."
    }


def test_a_document_with_no_columns_table_parses_to_nothing():
    """A malformed document is a gap to report, never an exception to handle."""
    parsed = kb_descriptions.parse_kb_document("# title\n\nfree text only\n")
    assert parsed == {"table": "", "columns": {}}


def test_empty_input_parses_to_nothing():
    """Guard the degenerate input rather than raising inside a build."""
    assert kb_descriptions.parse_kb_document("") == {"table": "", "columns": {}}


# ─── kb_builder: reading the document back ───────────────────────────────────


def test_read_kb_document_reads_the_key_write_produced():
    """Read and write must agree on the key, or the harvest silently finds nothing."""
    seen: Dict[str, str] = {}

    def _get(bucket: str, key: str) -> bytes:
        """Record the requested location and return the document."""
        seen.update(bucket=bucket, key=key)
        return KB_DOC.encode("utf-8")

    text = kb_builder.read_kb_document(
        semantic_layer_id="lay-1",
        semantic_layer_version="v2",
        database_name="acme",
        table_name="legalities",
        catalog_id="cat",
        bucket="b",
        s3_get=_get,
    )
    assert text == KB_DOC
    assert seen["key"] == kb_builder.kb_object_key(
        semantic_layer_id="lay-1",
        semantic_layer_version="v2",
        catalog_id="cat",
        database_name="acme",
        table_name="legalities",
    )


def test_read_kb_document_requires_the_layer_scope():
    """An unscoped read would fetch another layer's document."""
    with pytest.raises(ValueError, match="semantic_layer_id"):
        kb_builder.read_kb_document(
            semantic_layer_id="",
            semantic_layer_version="v1",
            database_name="acme",
            table_name="t",
            catalog_id="cat",
            bucket="b",
            s3_get=lambda _b, _k: b"",
        )


# ─── main: the build wiring ──────────────────────────────────────────────────


def test_harvest_reads_one_document_per_table(monkeypatch: pytest.MonkeyPatch):
    """Every table's descriptions are harvested, keyed by table name."""
    monkeypatch.setattr(
        kb_builder, "read_kb_document", lambda **_k: KB_DOC
    )
    harvested = creation_main._harvest_kb_descriptions(
        id="lay-1",
        version="v1",
        tables=[
            {"database": "acme", "table": "legalities", "catalogId": "cat"},
            {"database": "acme", "table": "cards", "catalogId": "cat"},
        ],
    )
    assert set(harvested) == {"legalities", "cards"}
    assert harvested["cards"]["columns"]["format"]


def test_harvest_is_fail_soft_per_table(monkeypatch: pytest.MonkeyPatch):
    """One unreadable document must not cost the other tables their descriptions.

    A hybrid layer built before this change has no KB document to read at all, and
    a structural rebuild of it must still succeed.
    """
    def _read(*, table_name: str, **_k: Any) -> str:
        """Fail for one table only."""
        if table_name == "cards":
            raise RuntimeError("NoSuchKey")
        return KB_DOC

    monkeypatch.setattr(kb_builder, "read_kb_document", _read)
    harvested = creation_main._harvest_kb_descriptions(
        id="lay-1",
        version="v1",
        tables=[
            {"database": "acme", "table": "legalities", "catalogId": "cat"},
            {"database": "acme", "table": "cards", "catalogId": "cat"},
        ],
    )
    assert set(harvested) == {"legalities"}


def test_edge_comment_records_whether_the_key_was_measured():
    """Whether a join key was MEASURED is invisible in the viewer otherwise."""
    edge = {
        "source_table": "cards",
        "source_column": "setcode",
        "target_table": "sets",
        "target_column": "code",
        "property_name": "hasSets",
        "rule": "camel_segment",
    }
    verified = creation_main._edge_comment(edge=edge, verified=True)
    assert "cards.setcode references sets.code" in verified
    assert "measured unique" in verified and "NOT measured" not in verified
    assert "NOT measured unique" in creation_main._edge_comment(
        edge=edge, verified=False
    )


def test_build_vkg_emits_the_harvested_descriptions(
    monkeypatch: pytest.MonkeyPatch,
):
    """End to end: the KB document's lines land in the loaded N-Quads."""
    captured: Dict[str, str] = {}

    monkeypatch.setattr(
        creation_main,
        "_glue_columns",
        lambda **_k: [
            {"name": "id", "type": "bigint", "comment": ""},
            {"name": "uuid", "type": "string", "comment": ""},
        ],
    )
    monkeypatch.setattr(kb_builder, "read_kb_document", lambda **_k: KB_DOC)
    monkeypatch.setattr(
        creation_main,
        "_load_vkg_into_neptune",
        lambda *, namespace, nquads: captured.update(nq=nquads),
    )
    monkeypatch.setattr(creation_main, "_save_vkg_to_s3", lambda **_k: "s3://b/k.nq")
    monkeypatch.setattr(creation_main, "_stamp_vkg_built", lambda **_k: None)

    creation_main._make_build_vkg(
        id="lay-1",
        version="v1",
        config={},
        tables=[
            {"database": "acme", "table": "legalities", "catalogId": "AWSDataCatalog"}
        ],
    )()

    nq: str = captured["nq"]
    comments: List[str] = [
        line for line in nq.splitlines() if "rdf-schema#comment" in line
    ]
    # One per class + one per described column.
    assert len(comments) == 3
    assert "Surrogate primary key for each legality record." in nq
    assert "one row per (card, format) pair." in nq
    # The prose sections still never reach the graph.
    assert "deck-building tools" not in nq
