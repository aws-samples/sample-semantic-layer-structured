"""Tests for the RAG Phase 2 slice builder (KB-chunk-driven)."""
import json
import textwrap

from agents.metadata_query_agent.tier2.rag_slice_builder import RagSliceBuilder


def _md_customers() -> str:
    """Markdown doc for db.customers — has columns + a join to db.policy."""
    return textwrap.dedent("""
    # AWSDataCatalog.db.customers

    ## Overview
    A row is one customer.

    ## Reference Tables
    - `db.policy`: JOIN db.policy p ON c.customer_id = p.customer_id

    ## Common Query Patterns
    - Active customers: SELECT * FROM customers WHERE status = 'A'

    ## Columns
    | Column | Type | Description |
    |--------|------|-------------|
    | customer_id | varchar | Primary key. |
    | first_name | varchar | Given name. |
    """).strip()


def _md_policy() -> str:
    """Markdown doc for db.policy — has columns + ACORD path."""
    return textwrap.dedent("""
    # AWSDataCatalog.db.policy

    ## ACORD Source Path
    PolicySummary/Risk/Location

    ## Columns
    | Column | Type | Description |
    |--------|------|-------------|
    | policy_id | varchar | Primary key. |
    | customer_id | varchar | FK to customer(customer_id). |
    """).strip()


def test_build_emits_json_slice_with_tables_columns_and_joins():
    chunks = {"db.customers": _md_customers(), "db.policy": _md_policy()}
    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: chunks,
        judge_fn=lambda payload: {"sufficient": True, "missing": []},
        token_counter=lambda s: len(s) // 4, budget=12000,
    )
    slice_text = b.build(candidates=["db.customers", "db.policy"], namespace="ns")
    payload = json.loads(slice_text)
    assert payload["tables"] == ["db.customers", "db.policy"]
    col_names = {(c["table_id"], c["name"]) for c in payload["columns"]}
    assert ("db.customers", "customer_id") in col_names
    assert ("db.policy", "policy_id") in col_names
    assert any(
        j["from"] == "db.customers" and j["to"] == "db.policy"
        for j in payload["joins"]
    )
    assert payload["acord_paths"]["db.policy"] == "PolicySummary/Risk/Location"
    assert payload["query_patterns"]


def test_expand_unions_missing_tables_into_lookup():
    """A judge-requested table with a real (fetchable) chunk is folded in."""
    chunks = {"a": _md_customers(), "b": _md_policy()}

    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: {
            t: chunks[t] for t in table_ids if t in chunks
        },
        judge_fn=lambda _: {"sufficient": True, "missing": []},
        token_counter=lambda s: 1, budget=12000,
    )
    b.build(candidates=["a"], namespace="ns")
    slice_text = b.expand(slice_text="x", missing=["b"])
    payload = json.loads(slice_text)
    # "b" was fetchable, so it joins the slice with its columns.
    assert payload["tables"] == ["a", "b"]


def test_expand_drops_dotted_column_pseudo_tables():
    """Dotted ``table.column`` requests for nonexistent columns never enter tables[].

    The judge returns ``missing`` like ``normalized.fa.amount`` — a column that does
    not exist. ``chunks_lookup`` has no chunk for that dotted id, so it must be dropped
    rather than appended to ``tables[]`` as a phantom join target.
    """
    chunks = {"normalized.fa": _md_customers()}  # only the real table is fetchable

    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: {
            t: chunks[t] for t in table_ids if t in chunks
        },
        judge_fn=lambda _: {"sufficient": False, "missing": []},
        token_counter=lambda s: len(s) // 4, budget=12000,
    )
    b.build(candidates=["normalized.fa"], namespace="ns")
    slice_text = b.expand(
        slice_text="x",
        missing=["normalized.fa.amount", "normalized.fa.activity_date"],
    )
    payload = json.loads(slice_text)
    assert "normalized.fa.amount" not in payload["tables"]
    assert "normalized.fa.activity_date" not in payload["tables"]
    assert payload["tables"] == ["normalized.fa"]


def _md_with_cols(table_id: str, cols: list, desc_len: int = 0) -> str:
    """Build a minimal table doc with the given columns (optionally fat descriptions)."""
    desc = "x" * desc_len
    lines = [
        f"# {table_id}",
        "",
        "## Columns",
        "| Column | Type | Description |",
        "|--------|------|-------------|",
    ]
    lines += [f"| {c} | varchar | {desc} |" for c in cols]
    return "\n".join(lines)


def test_fit_strips_descriptions_before_dropping_columns():
    """Over budget: column names/types survive; only descriptions are shed."""
    # Two tables, each with fat per-column descriptions → over budget with
    # descriptions, comfortably under once they're stripped.
    chunks = {
        "db.target": _md_with_cols("db.target", ["amount", "activity_date"], desc_len=400),
        "db.other": _md_with_cols("db.other", ["x", "y"], desc_len=400),
    }
    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: {
            t: chunks[t] for t in table_ids if t in chunks
        },
        judge_fn=lambda _: {"sufficient": True, "missing": []},
        token_counter=lambda s: len(s) // 4, budget=200,
    )
    slice_text = b.build(candidates=["db.target", "db.other"], namespace="ns")
    payload = json.loads(slice_text)
    col_names = {(c["table_id"], c["name"]) for c in payload["columns"]}
    # Every column NAME is retained (descriptions dropped, not columns).
    assert ("db.target", "amount") in col_names
    assert ("db.target", "activity_date") in col_names
    assert all("description" not in c for c in payload["columns"])


def test_fit_evicts_least_relevant_table_not_lowest_degree():
    """When columns must be dropped, the FIRST (most-relevant) candidate keeps its
    columns and the LAST (least-relevant) loses them — regardless of join degree.

    Regression guard for the eviction-order bug: the target leaf table
    (financial_activity, low degree, high relevance) was being evicted before
    peripheral hub tables. Eviction must walk candidates in reverse-relevance.
    """
    # target = most-relevant (router rank 0), low column count; filler tables are
    # less relevant. Tiny budget forces dropping one table's columns entirely.
    chunks = {
        "db.target": _md_with_cols("db.target", ["amount", "activity_date"]),
        "db.filler": _md_with_cols(
            "db.filler", [f"c{i}" for i in range(40)]),
    }
    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: {
            t: chunks[t] for t in table_ids if t in chunks
        },
        judge_fn=lambda _: {"sufficient": True, "missing": []},
        token_counter=lambda s: len(s) // 4, budget=120,
    )
    # Candidate order = relevance: target first, filler last.
    slice_text = b.build(candidates=["db.target", "db.filler"], namespace="ns")
    payload = json.loads(slice_text)
    tables_with_cols = {c["table_id"] for c in payload["columns"]}
    # The most-relevant table keeps its columns; the least-relevant is evicted.
    assert "db.target" in tables_with_cols
    assert "db.filler" not in tables_with_cols


def test_expand_pulls_in_real_unrouted_table():
    """A ``missing`` entry naming a real-but-unrouted table is pulled in with columns."""
    chunks = {"db.customers": _md_customers(), "db.policy": _md_policy()}

    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: {
            t: chunks[t] for t in table_ids if t in chunks
        },
        judge_fn=lambda _: {"sufficient": False, "missing": []},
        token_counter=lambda s: len(s) // 4, budget=12000,
    )
    b.build(candidates=["db.customers"], namespace="ns")
    slice_text = b.expand(slice_text="x", missing=["db.policy"])
    payload = json.loads(slice_text)
    assert "db.policy" in payload["tables"]
    col_names = {(c["table_id"], c["name"]) for c in payload["columns"]}
    assert ("db.policy", "policy_id") in col_names


def test_expand_no_fetchable_missing_leaves_tables_unchanged():
    """When no ``missing`` entry resolves to a fetchable table, tables[] is unchanged."""
    chunks = {"db.customers": _md_customers()}

    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: {
            t: chunks[t] for t in table_ids if t in chunks
        },
        judge_fn=lambda _: {"sufficient": False, "missing": []},
        token_counter=lambda s: len(s) // 4, budget=12000,
    )
    before = json.loads(b.build(candidates=["db.customers"], namespace="ns"))
    after = json.loads(
        b.expand(slice_text="x", missing=["db.ghost.col", "db.nonexistent"]))
    assert after["tables"] == before["tables"] == ["db.customers"]


def test_slice_dict_tables_only_lists_fetched_ids():
    """A candidate whose chunk is missing is excluded from tables[]."""
    chunks = {"db.customers": _md_customers()}  # "db.bogus" has no chunk

    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: {
            t: chunks[t] for t in table_ids if t in chunks
        },
        judge_fn=lambda _: {"sufficient": True, "missing": []},
        token_counter=lambda s: len(s) // 4, budget=12000,
    )
    slice_text = b.build(candidates=["db.customers", "db.bogus"], namespace="ns")
    payload = json.loads(slice_text)
    assert payload["tables"] == ["db.customers"]


def test_fit_drops_columns_when_over_budget():
    """Tiny budget forces column truncation; joins/tables stay intact."""
    chunks = {"t": _md_customers()}
    b = RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: chunks,
        judge_fn=lambda _: {"sufficient": True, "missing": []},
        token_counter=lambda s: len(s),  # 1 token per char
        budget=200,
    )
    slice_text = b.build(candidates=["t"], namespace="ns")
    payload = json.loads(slice_text)
    assert payload["tables"] == ["t"]
    # columns are dropped first to fit the tiny budget
    assert payload["columns"] == [] or len(payload["columns"]) <= 1
