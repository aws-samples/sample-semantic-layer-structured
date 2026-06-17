"""Unit tests for the ontology query-agent schema digest.

The supervisor loop (``supervisor.py`` / ``supervisor_runner.py``) was removed
when both query agents migrated to the Strands graph workflow — its tests are
gone with it. The schema digest survives (the ``disambiguate_query_terms`` tool
still builds it), so its tests remain here.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents"))


def test_schema_digest_includes_class_and_columns():
    from ontology_query_agent.schema_digest import build_schema_digest

    ontology = {
        "classes": {
            "http://ex.com/Customer": {
                "label": "Customer",
                "comment": "A person who holds at least one policy",
            }
        },
        "properties": {
            "http://ex.com/firstName": {
                "label": "first_name",
                "comment": "Customer's given name",
            }
        },
        "mappings": {
            "http://ex.com/Customer": {"table": "db.customers"},
            "http://ex.com/firstName": {"column": "customers.first_name"},
        },
    }
    digest = build_schema_digest(ontology)
    assert "TABLE db.customers (Customer)" in digest
    assert "A person who holds at least one policy" in digest
    assert "first_name" in digest
    assert "Customer's given name" in digest


def test_schema_digest_skips_classes_without_table_mapping():
    from ontology_query_agent.schema_digest import build_schema_digest

    ontology = {
        "classes": {
            "http://ex.com/Abstract": {"label": "Abstract"},
        },
        "properties": {},
        "mappings": {},
    }
    digest = build_schema_digest(ontology)
    assert "Abstract" not in digest
    assert digest == ""


def test_schema_digest_truncates_huge_ontologies():
    from ontology_query_agent.schema_digest import (
        MAX_DIGEST_CHARS,
        build_schema_digest,
    )

    classes = {
        f"http://ex.com/Class{i}": {"label": f"Class{i}", "comment": "X" * 200}
        for i in range(500)
    }
    mappings = {f"http://ex.com/Class{i}": {"table": f"db.t{i}"} for i in range(500)}
    digest = build_schema_digest({"classes": classes, "properties": {}, "mappings": mappings})
    assert len(digest) <= MAX_DIGEST_CHARS + 100  # plus the truncation marker
    assert "[truncated]" in digest


def test_schema_digest_from_json_handles_invalid_input():
    from ontology_query_agent.schema_digest import build_schema_digest_from_json

    assert build_schema_digest_from_json("") == ""
    assert build_schema_digest_from_json("not-json") == ""
