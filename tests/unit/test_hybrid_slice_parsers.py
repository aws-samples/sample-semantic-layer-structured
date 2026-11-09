"""Hybrid slice parsers against the documents the creation agent really writes.

``slice_builder``'s parsers were copied from ``metadata_query_agent.tier2``, whose
KB documents are written by ``metadata_agent.prompt_builder`` in a ONE-LINE shape:

    - `ref_status`: JOIN ref_status s ON t.status_cd = s.status_cd

The hybrid layer's documents are written by
``semantic_layer_creation_agent.query_prompts.KB_DOCUMENT_FORMAT``, which puts the
target and its FK on an arrow bullet and the JOIN in a following fenced ``sql``
block. The copied bullet regex matches neither, so joins silently parsed to
NOTHING: measured against the live 40-document ``hybrid-curated_layer`` corpus,
only 3 of 40 documents yielded a single edge while 502 of 502 columns parsed.

A slice with no ``joins`` gives the generator no way to know which key relates two
tables, so multi-table questions get invented join keys or an abstain. These tests
pin BOTH formats — the reference one-liner must keep working.
"""
from __future__ import annotations

import textwrap

from agents.semantic_layer_query_agent.slice_builder import _parse_reference_joins


# Verbatim structure of the live ``normalized/life_participant.md`` Reference
# Tables section: outbound arrow bullets, then inbound bullets whose join is a
# multi-condition ON. SQL here is inert fixture text, not executed (nosec B608).
FENCED_DOC = textwrap.dedent("""
    # catalog.normalized.life_participant

    ## Reference Tables

    **Outbound (this table's FK -> parent):**
    - `holding_id` -> `holding.holding_id` (the policy this participation belongs to)
      ```sql
      SELECT lp.*, h.*
      FROM life_participant lp
      JOIN holding h ON lp.holding_id = h.holding_id
      WHERE lp.is_deleted = false
      ```
    - `party_id` -> `party.party_id` (the person/entity in this role)
      ```sql
      SELECT lp.*, p.*
      FROM life_participant lp
      JOIN party p ON lp.party_id = p.party_id
      ```

    **Inbound (child tables referencing this table's entities):**
    - `rider_participant` relates parties to riders on a `holding`; join through the
      shared `holding_id`/`party_id` pair:
      ```sql
      SELECT lp.*, rp.*
      FROM life_participant lp
      JOIN rider_participant rp
        ON lp.holding_id = rp.holding_id AND lp.party_id = rp.party_id
      ```
    - `relation` (party-to-party relationships) can be bridged via `party_id` when a
      question needs to know how two participants on the same holding relate.

    ## Columns
    | Column | Type | Description |
    |---|---|---|
    | holding_id | string | FK to holding. |
    """).strip()  # nosec B608 — inert markdown test fixture, not executed SQL

# The reference (metadata_agent) one-line shape, which must keep parsing.
ONELINE_DOC = textwrap.dedent("""
    # AWSDataCatalog.ins.policy

    ## Reference Tables
    - `ref_status`: JOIN ref_status s ON t.status_cd = s.status_cd
    """).strip()  # nosec B608 — inert markdown test fixture, not executed SQL

TID = "normalized.life_participant"


def _edges(md: str, table_id: str = TID):
    """Parse joins and return them as ``(to, from_col, to_col)`` tuples.

    Parameters:
        md: The KB markdown document body to parse.
        table_id: The ``{database}.{table}`` id the joins originate from.
    Returns:
        list: One ``(to, from_col, to_col)`` tuple per parsed edge, in order.
    """
    return [
        (e["to"], e["from_col"], e["to_col"])
        for e in _parse_reference_joins(md=md, table_id=table_id)
    ]


def test_fenced_sql_block_yields_the_join_edge():
    """The dominant hybrid shape: arrow bullet + fenced JOIN must produce an edge."""
    edges = _edges(FENCED_DOC)

    assert ("holding", "holding_id", "holding_id") in edges
    assert ("party", "party_id", "party_id") in edges


def test_every_fenced_block_in_the_section_is_parsed():
    """All four outbound/inbound blocks contribute, not just the first."""
    targets = {to for to, _, _ in _edges(FENCED_DOC)}

    assert targets == {"holding", "party", "rider_participant"}


def test_multi_condition_on_yields_one_edge_per_key_pair():
    """A composite join key must surface BOTH pairs, not silently drop one.

    ``ON lp.holding_id = rp.holding_id AND lp.party_id = rp.party_id`` is how a
    bridge table is joined; a generator told only about ``holding_id`` produces a
    cartesian-ish result across parties.
    """
    rider = [e for e in _edges(FENCED_DOC) if e[0] == "rider_participant"]

    assert sorted(rider) == [
        ("rider_participant", "holding_id", "holding_id"),
        ("rider_participant", "party_id", "party_id"),
    ]


def test_from_col_is_this_tables_side_of_the_on_clause():
    """Column orientation must follow the alias map, not ON left-to-right order.

    The base table is not always on the left of the equality, so resolving the
    aliases is what keeps ``from_col`` this table's column.
    """
    md = FENCED_DOC.replace(
        "JOIN holding h ON lp.holding_id = h.holding_id",
        "JOIN holding h ON h.holding_id = lp.lp_holding_ref",
    )

    edges = [e for e in _edges(md) if e[0] == "holding"]

    assert edges == [("holding", "lp_holding_ref", "holding_id")]


def test_bullet_without_a_sql_block_yields_no_edge():
    """An unverified "likely follows the same pattern" bullet must not invent a join.

    ``relation`` is described in prose with no JOIN, so there is no key to report;
    emitting a bare edge would hand the generator a join it cannot write.
    """
    assert "relation" not in {to for to, _, _ in _edges(FENCED_DOC)}


def test_prose_bullet_with_a_colon_is_not_a_join():
    """``- Inbound: to find all payouts...`` must not invent a table named "Inbound".

    The copied one-line regex keys only on ``- <word>: <text>``, so any prose bullet
    with a colon became an edge. Measured on the live corpus this produced three
    phantom targets (``Inbound``) — a join target that does not exist in the layer,
    handed to the SQL generator as if it did.
    """
    md = textwrap.dedent("""
        # catalog.normalized.holding_payout

        ## Reference Tables

        - Outbound: `holding_id` identifies the holding this payout belongs to.
          - Inbound: to find all payout markers for a given holding, filter
            `holding_payout` by `holding_id` matching the `holding` row of interest.
        """).strip()

    assert _parse_reference_joins(md=md, table_id="normalized.holding_payout") == []


def test_prose_bullet_mentioning_the_word_join_is_still_not_a_join():
    """A bullet whose PROSE says "joins" must not become an edge either.

    ``- Inbound: any table that joins to `holding` can be related by bridging ...``
    contains the word, so a "does it mention JOIN" test still fabricates a target
    called "Inbound". The bullet's own target must appear in an actual JOIN clause.
    The bridging JOIN in the following inline span is a real edge and stays.
    """
    md = textwrap.dedent("""
        # catalog.normalized.holding_dbg

        ## Reference Tables

        - Inbound: any table that joins to `holding` can be related to `holding_dbg`
          only by bridging through `holding`:
          `JOIN holding h ON holding_dbg.holding_id = h.holding_id`
        """).strip()  # nosec B608 — inert markdown test fixture

    edges = _edges(md, table_id="normalized.holding_dbg")

    assert "Inbound" not in {to for to, _, _ in edges}
    assert edges == [("holding", "holding_id", "holding_id")]


def test_reference_oneline_bullet_format_still_parses():
    """The copied metadata_agent shape must keep working — both formats ship."""
    edges = _parse_reference_joins(md=ONELINE_DOC, table_id="ins.policy")

    assert len(edges) == 1
    assert edges[0]["to"] == "ref_status"
    assert edges[0]["from_col"] == "status_cd"
    assert edges[0]["to_col"] == "status_cd"
    assert edges[0]["sql"].startswith("JOIN ref_status s ON")


def test_edges_carry_the_join_sql_for_the_generator():
    """``sql`` must hold the actual JOIN clause the generator can copy."""
    edge = next(
        e for e in _parse_reference_joins(md=FENCED_DOC, table_id=TID)
        if e["to"] == "holding"
    )

    assert edge["from"] == TID
    assert edge["sql"].upper().startswith("JOIN HOLDING")
    assert "lp.holding_id = h.holding_id" in edge["sql"]


def test_duplicate_key_pairs_are_reported_once():
    """The same edge described twice (bullet text + block) must not double-count."""
    md = FENCED_DOC + "\n\n## Notes\nnothing here\n"

    edges = _parse_reference_joins(md=md, table_id=TID)

    assert len(edges) == len({(e["to"], e["from_col"], e["to_col"]) for e in edges})


def test_no_reference_tables_section_returns_empty():
    """A document without the section yields no edges rather than raising."""
    assert _parse_reference_joins(md="# t\n\n## Overview\nx", table_id=TID) == []


# --------------------------------------------------------------------------- #
# INBOUND joins on a parent/lookup table. A leaf reference table has no outbound  #
# FK, so its document describes only how CHILDREN join TO it — putting the base   #
# table on the JOIN side and the child in the FROM. Orienting by "the joined      #
# table is the target" mistakes those for self-joins and drops every edge, which  #
# is exactly what left `admin_codes`, `coverage_product` and `distribution_level` #
# at zero joins in the live corpus. Orient by which ON side resolves to the base  #
# table instead.                                                                  #
# --------------------------------------------------------------------------- #
INBOUND_DOC = textwrap.dedent("""
    # catalog.normalized.coverage_product

    ## Reference Tables

    This table is the **parent (lookup)**; it has no outbound foreign keys.

    **Inbound joins (child tables referencing this table):**

    - `coverage` — each coverage instance is written against a coverage product:
      ```sql
      SELECT c.*, cp.product_name
      FROM coverage c
      JOIN coverage_product cp ON c.product_code = cp.product_code
      ```
    """).strip()  # nosec B608 — inert markdown test fixture, not executed SQL


def test_inbound_join_on_a_lookup_table_yields_an_edge():
    """The base table on the JOIN side is an inbound edge, not a self-join."""
    edges = _edges(INBOUND_DOC, table_id="normalized.coverage_product")

    assert edges == [("coverage", "product_code", "product_code")]


def test_inbound_join_with_a_literal_filter_condition_still_parses():
    """A composite ON mixing a key equality with a literal filter yields the key.

    ``ON ac.code_value = a.state_code AND ac.admin_code_type = 'STATE'`` — the
    second condition is a filter, not a join key, and must not produce an edge.
    """
    md = textwrap.dedent("""
        # catalog.normalized.admin_codes

        ## Reference Tables

        **Inbound usage (conceptual):**

        ```sql
        SELECT a.*, ac.abbreviation AS state_label
        FROM address a
        JOIN admin_codes ac
          ON ac.code_value = a.state_code
         AND ac.admin_code_type = 'STATE'
        ```
        """).strip()  # nosec B608 — inert markdown test fixture

    assert _edges(md, table_id="normalized.admin_codes") == [
        ("address", "code_value", "state_code"),
    ]


def test_key_transform_join_resolves_both_columns():
    """A join wrapping one side in a function must still report both columns.

    ``ON REPLACE(dl.party_id, 'PARTY#', '') = p.party_id`` is a real pattern in the
    corpus (prefixed vs native surface form). These are precisely the joins a
    generator gets wrong unqided, so the edge — and its ``sql``, which carries the
    transform — must reach the slice.
    """
    md = textwrap.dedent("""
        # catalog.normalized.distribution_level

        ## Reference Tables

        **Outbound (this table -> parent):**
        - `party_id` -> `party.party_id` — strip the `'PARTY#'` prefix before joining.
          ```sql
          SELECT dl.*, p.*
          FROM distribution_level dl
          JOIN party p
            ON REPLACE(dl.party_id, 'PARTY#', '') = p.party_id
          WHERE dl.is_deleted = false;
          ```
        """).strip()  # nosec B608 — inert markdown test fixture

    edges = _parse_reference_joins(
        md=md, table_id="normalized.distribution_level",
    )

    assert [(e["to"], e["from_col"], e["to_col"]) for e in edges] == [
        ("party", "party_id", "party_id"),
    ]
    # The transform itself must survive into the SQL the generator copies.
    assert "REPLACE(dl.party_id, 'PARTY#', '')" in edges[0]["sql"]


# --------------------------------------------------------------------------- #
# INLINE backtick JOIN spans. Not every document fences its SQL — several put the  #
# whole JOIN in a one-line code span under a nested bullet, and refer to the base  #
# table by its own NAME rather than an alias (no FROM clause at all). Reading only #
# fenced blocks, and resolving qualifiers only from FROM/JOIN, left `holding`,     #
# `holding_activity`, `holding_projection` and `life_detail` at zero joins — and   #
# `holding` is the hub table almost every multi-table question needs.              #
# --------------------------------------------------------------------------- #
INLINE_DOC = textwrap.dedent("""
    # catalog.normalized.life_detail

    ## Reference Tables

    **Outbound (life_detail -> parent):**
    - `holding` — the core policy/holding entity.
      - `JOIN holding h ON life_detail.holding_id = h.holding_id`

    **Inbound (child tables that hang off the same holding):**
    - `life_participant` — parties associated with the life policy.
      - `JOIN life_participant lp ON lp.holding_id = life_detail.holding_id`
    """).strip()  # nosec B608 — inert markdown test fixture, not executed SQL


def test_inline_backtick_join_span_yields_an_edge():
    """A JOIN written in a one-line code span counts the same as a fenced one."""
    edges = _edges(INLINE_DOC, table_id="normalized.life_detail")

    assert ("holding", "holding_id", "holding_id") in edges
    assert ("life_participant", "holding_id", "holding_id") in edges


def test_base_table_referenced_by_its_own_name_resolves():
    """A qualifier equal to the base table name is THIS table, alias or not.

    ``JOIN holding h ON life_detail.holding_id = h.holding_id`` has no ``FROM``, so
    the only way to know ``life_detail.`` is this table is the document's own id.
    """
    md = textwrap.dedent("""
        # catalog.normalized.holding

        ## Reference Tables

        - **holding_activity** (transactions on a holding)
          `SELECT * FROM holding h JOIN holding_activity ha ON ha.holding_id = h.sk`
        """).strip()  # nosec B608 — inert markdown test fixture

    # ``h.sk`` is the base side even though it is written on the RIGHT of the ON.
    assert _edges(md, table_id="normalized.holding") == [
        ("holding_activity", "sk", "holding_id"),
    ]


def test_placeholder_table_in_a_generic_pattern_yields_no_edge():
    """A documented ``<business_table>`` decode template names no real table.

    ``type_codes`` documents its inbound joins as a template with placeholders; there
    is no concrete table or column to report, and inventing one would be a fabricated
    join key.
    """
    md = textwrap.dedent("""
        # catalog.normalized.type_codes

        ## Reference Tables

        The generic decode pattern is:

        ```sql
        SELECT b.*, tc.code_description
        FROM <business_table> b
        JOIN type_codes tc
          ON tc.code_type  = '<Domain>'
         AND tc.code_value = b.<coded_column>
        ```
        """).strip()  # nosec B608 — inert markdown test fixture

    assert _edges(md, table_id="normalized.type_codes") == []


def test_join_between_two_other_tables_yields_no_edge():
    """A bridge-path example that never mentions the base table adds nothing.

    Attributing someone else's join to this table would give the generator a key
    this table does not have.
    """
    md = textwrap.dedent("""
        # catalog.normalized.address

        ## Reference Tables

        Bridge path example (not a join on this table):
        ```sql
        SELECT * FROM party p JOIN holding h ON p.party_id = h.party_id
        ```
        """).strip()  # nosec B608 — inert markdown test fixture

    assert _edges(md, table_id="normalized.address") == []
