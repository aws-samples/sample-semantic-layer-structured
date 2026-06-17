"""Unit tests for the markdown slice parser.

The parser consumes the markdown documents written by
``agents.metadata_agent.save_metadata_document_to_s3`` and returns the
slice components (columns / reference-table joins / ACORD path / query
patterns) that the RAG slice builder used to fetch from Glue.
"""
from __future__ import annotations

import textwrap

from agents.metadata_query_agent.tier2.markdown_slice_parser import (
    parse_acord_path,
    parse_columns,
    parse_query_patterns,
    parse_reference_joins,
)


def _doc(table_id: str = "AWSDataCatalog.ins.policy") -> str:
    """Return a representative markdown doc covering all sections.

    The SQL strings in the Query Patterns / Reference Tables sections are inert
    test-fixture text, not executed SQL (nosec B608).
    """
    return textwrap.dedent(f"""
    # {table_id}

    ## Overview
    A row is one insurance policy. Used by underwriting and claims teams.

    ## Business Purpose
    Source-of-record for active and historical policies.

    ## ACORD Source Path
    PolicySummary/Risk/Location

    ## Reference Tables
    - `ref_coverage_type`: JOIN ref_coverage_type r ON t.coverage_type_cd = r.coverage_type_cd
    - `ref_status`: JOIN ref_status s ON t.status_cd = s.status_cd

    ## Common Query Patterns
    - Active policies by product: SELECT * FROM policy WHERE status = 'A' AND product_cd = ?
    - Lapsed policy count: SELECT COUNT(*) FROM policy WHERE status = 'L'

    ## Columns
    | Column | Type | Description |
    |--------|------|-------------|
    | policy_id | varchar | Primary key. |
    | customer_id | varchar | FK to customer(customer_id). |
    | status_cd | varchar | One of A/L/T (active, lapsed, terminated). |

    ## Sample Data
    irrelevant for tests
    """).strip()


def test_parse_columns_returns_table_id_name_type_description():
    rows = parse_columns(md=_doc(), table_id="AWSDataCatalog.ins.policy")
    names = [r["name"] for r in rows]
    assert names == ["policy_id", "customer_id", "status_cd"]
    assert rows[0] == {
        "table_id": "AWSDataCatalog.ins.policy",
        "name": "policy_id",
        "type": "varchar",
        "description": "Primary key.",
    }


def test_parse_columns_handles_missing_section():
    md = "# t\n\n## Overview\nno columns here"
    assert parse_columns(md=md, table_id="t") == []


def test_parse_reference_joins_emits_from_to_sql():
    joins = parse_reference_joins(md=_doc(), table_id="AWSDataCatalog.ins.policy")
    assert len(joins) == 2
    first = joins[0]
    assert first["from"] == "AWSDataCatalog.ins.policy"
    assert first["to"] == "ref_coverage_type"
    assert first["sql"].startswith("JOIN ref_coverage_type r ON")
    assert first["from_col"] == "coverage_type_cd"
    assert first["to_col"] == "coverage_type_cd"


def test_parse_reference_joins_no_section_returns_empty():
    md = "# t\n## Overview\nx"
    assert parse_reference_joins(md=md, table_id="t") == []


def test_parse_acord_path_returns_path():
    assert parse_acord_path(md=_doc()) == "PolicySummary/Risk/Location"


def test_parse_acord_path_missing_returns_none():
    assert parse_acord_path(md="# t\n## Overview\nno acord") is None


def test_parse_query_patterns_returns_list():
    pats = parse_query_patterns(md=_doc())
    assert len(pats) == 2
    assert "Active policies by product" in pats[0]
