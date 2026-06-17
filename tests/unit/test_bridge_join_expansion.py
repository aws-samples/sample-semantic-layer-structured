"""Multi-hop bridge join expansion (Cluster C).

The 2026-06-08 review found "total market value of all active holdings, grouped
by party" cannot be expressed with only ``holding`` + ``party`` in the slice:
they do not join directly — they bridge through ``coverage``
(``holding.holding_id = coverage.holding_id`` and ``coverage.party_id`` ↔
``party.party_id``). The slice builder must discover the bridge table from the
reference-join edges and pull it (and the transitive joins) into the slice.

These tests pin the builder's ``bridge_table_candidates`` discovery (pure, no I/O)
and that ``build`` auto-adds a fetchable bridge so the two endpoints connect.
"""
from __future__ import annotations

import json
import textwrap

from agents.metadata_query_agent.tier2.rag_slice_builder import RagSliceBuilder


def _md_holding() -> str:
    return textwrap.dedent("""
    # normalized.holding

    ## Reference Tables
    - `normalized.coverage`: JOIN normalized.coverage c ON h.holding_id = c.holding_id

    ## Columns
    | Column | Type | Description |
    |--------|------|-------------|
    | holding_id | varchar | Primary key. |
    | market_value | double | Market value. |
    | holding_status | varchar | Active/Inactive. |
    """).strip()


def _md_coverage() -> str:
    return textwrap.dedent("""
    # normalized.coverage

    ## Reference Tables
    - `normalized.holding`: JOIN normalized.holding h ON c.holding_id = h.holding_id
    - `normalized.party`: JOIN normalized.party p ON c.party_id = p.party_id

    ## Columns
    | Column | Type | Description |
    |--------|------|-------------|
    | coverage_id | varchar | Primary key. |
    | holding_id | varchar | FK to holding. |
    | party_id | varchar | FK to party. |
    """).strip()


def _md_party() -> str:
    return textwrap.dedent("""
    # normalized.party

    ## Columns
    | Column | Type | Description |
    |--------|------|-------------|
    | party_id | varchar | Primary key. |
    | party_type | varchar | Individual/Org/Trust. |
    """).strip()


_ALL_CHUNKS = {
    "normalized.holding": _md_holding(),
    "normalized.coverage": _md_coverage(),
    "normalized.party": _md_party(),
}


def _builder(chunks: dict, judge=None):
    return RagSliceBuilder(
        chunks_lookup=lambda *, table_ids, namespace: {
            t: chunks[t] for t in table_ids if t in chunks},
        judge_fn=judge or (lambda payload: {"sufficient": True, "missing": []}),
        token_counter=lambda s: len(s) // 4, budget=100000,
    )


def test_bridge_table_candidates_finds_connecting_table() -> None:
    # holding and party do not join directly; coverage connects both. The bridge
    # discovery (given the two endpoints' join edges naming coverage) must surface
    # coverage as a fetchable bridge.
    b = _builder(_ALL_CHUNKS)
    bridges = b.bridge_table_candidates(
        endpoints=["normalized.holding", "normalized.party"],
        namespace="ns",
    )
    assert "normalized.coverage" in bridges


def test_bridge_not_added_when_endpoints_join_directly() -> None:
    # If two endpoints already share a join edge, no bridge is needed.
    chunks = {
        "normalized.holding": _md_holding(),
        "normalized.coverage": _md_coverage(),
    }
    b = _builder(chunks)
    bridges = b.bridge_table_candidates(
        endpoints=["normalized.holding", "normalized.coverage"],
        namespace="ns",
    )
    assert bridges == []


def test_build_auto_adds_bridge_so_endpoints_connect() -> None:
    # Building a slice from just {holding, party} must pull coverage in so the
    # slice carries a connecting join path (holding↔coverage↔party).
    b = _builder(_ALL_CHUNKS)
    slice_text = b.build(
        candidates=["normalized.holding", "normalized.party"], namespace="ns")
    payload = json.loads(slice_text)
    assert "normalized.coverage" in payload["tables"]
    edges = {(j["from"], j["to"]) for j in payload["joins"]}
    # Both hops of the bridge are present (in either direction).
    assert (("normalized.holding", "normalized.coverage") in edges
            or ("normalized.coverage", "normalized.holding") in edges)
    assert (("normalized.coverage", "normalized.party") in edges
            or ("normalized.party", "normalized.coverage") in edges)


def test_build_no_bridge_when_unavailable_is_noop() -> None:
    # If the bridge table's chunk can't be fetched, build must not fail — it just
    # returns the endpoints without a bridge (degrade, don't crash).
    chunks = {"normalized.holding": _md_holding(), "normalized.party": _md_party()}
    b = _builder(chunks)  # coverage chunk absent
    slice_text = b.build(
        candidates=["normalized.holding", "normalized.party"], namespace="ns")
    payload = json.loads(slice_text)
    assert "normalized.coverage" not in payload["tables"]
    assert set(payload["tables"]) == {"normalized.holding", "normalized.party"}


def test_bridge_discovered_inbound_when_neither_endpoint_names_it() -> None:
    # Real failure (nb2 2026-06-12): for "market value of holdings BY party",
    # holding's doc names only `policy` and party's doc names nothing relevant —
    # NEITHER endpoint names `coverage`. But coverage's OWN doc declares joins to
    # BOTH holding and party. The old discovery only walked endpoints' OUTBOUND
    # edges, so coverage (an INBOUND bridge) was never surfaced. With coverage in
    # the candidate pool, bidirectional discovery must find it.
    holding_md = textwrap.dedent("""
    # normalized.holding
    ## Reference Tables
    - `policy`: JOIN policy p ON holding.policy_id = p.policy_id
    ## Columns
    | Column | Type | Description |
    |--------|------|-------------|
    | holding_id | varchar | PK. |
    | market_value | double | Value. |
    """).strip()
    chunks = {
        "normalized.holding": holding_md,
        "normalized.coverage": _md_coverage(),
        "normalized.party": _md_party(),
    }
    b = _builder(chunks)
    # coverage is in the candidate set (Phase 1 retrieved it) but neither endpoint
    # names it; party & holding do not join directly.
    b._candidates = ["normalized.holding", "normalized.party", "normalized.coverage"]
    bridges = b.bridge_table_candidates(
        endpoints=["normalized.holding", "normalized.party"], namespace="ns")
    assert "normalized.coverage" in bridges, (
        "coverage bridges holding↔party via its OWN inbound join edges; "
        "discovery must consider candidate-pool tables, not only outbound edges")


def test_expand_rediscovers_bridge_for_judge_added_endpoint() -> None:
    # Real failure (nb2 2026-06-12): Phase 1 retrieves only party-centric tables,
    # so build() sees a single endpoint and discovers no bridge. The judge then
    # asks for the second endpoint (holding) via missing[]. expand() must re-run
    # bridge discovery over the WIDENED set and pull in coverage — otherwise the
    # next judge round still sees an unconnectable holding↔party pair and degrades.
    b = _builder(_ALL_CHUNKS)
    # build from a party-only slice: nothing to bridge yet.
    built = b.build(candidates=["normalized.party"], namespace="ns")
    assert "normalized.coverage" not in json.loads(built)["tables"]
    # judge says holding is missing → expand folds it in AND must re-discover the
    # coverage bridge that now connects holding↔party.
    expanded = b.expand(slice_text=built, missing=["normalized.holding"])
    payload = json.loads(expanded)
    assert "normalized.holding" in payload["tables"]
    assert "normalized.coverage" in payload["tables"], (
        "expand() must re-run bridge discovery so the judge-added endpoint "
        "connects through coverage")
    # the re-discovered bridge is protected from budget eviction
    assert "normalized.coverage" in b._bridges
    edges = {(j["from"], j["to"]) for j in payload["joins"]}
    assert (("normalized.holding", "normalized.coverage") in edges
            or ("normalized.coverage", "normalized.holding") in edges)
    assert (("normalized.coverage", "normalized.party") in edges
            or ("normalized.party", "normalized.coverage") in edges)
