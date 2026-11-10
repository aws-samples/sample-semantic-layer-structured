"""Bridge generated SQL into ontology ``(subject_class_iri, predicate_iri)`` pairs.

This is the SQL analog of extracting BGP triples from SPARQL: it maps the
physical table/column references in a generated SQL statement to the ontology
class and predicate IRIs they correspond to, using the inverse Ontop mapping
index built from the thin VKG (:mod:`agents.semantic_layer_query_agent.ontop_mappings`).
The resulting triples are what the ontology grounding gate consumes.

Design constraints (enforced by ``tests/unit/test_hybrid_isolation.py``):
    * Import only the standard library, ``sqlglot``, ``rdflib``, and this
      package's own ``ontop_mappings`` module.
    * Never depend on ``agents.shared`` or any sibling agent package.

The alias-resolution approach (``exp.Table`` alias/name map, and skipping
SELECT-output aliases + CTE names so an aggregate alias re-referenced in
``ORDER BY``/``HAVING`` isn't treated as a schema column) is copied — not
imported — from ``agents/metadata_query_agent/tier2/grounding.py``.

Fail-soft principle: an unmapped column is recorded in ``unmapped`` and never
produces a triple, so the bridge can never emit a *false* ontology reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import sqlglot
from sqlglot import exp

from .ontop_mappings import MappingIndex


@dataclass
class BridgeResult:
    """Outcome of mapping one SQL statement into ontology triples.

    Attributes:
        triples: Ordered, de-duplicated list of
            ``(subject_class_iri_or_None, predicate_iri)`` pairs. The subject
            class IRI is ``None`` when the owning table has no ``mapsToTable``
            class mapping even though the column mapped to a predicate.
        unmapped: Physical column references (``"table.column"`` or bare column
            name) that could not be resolved to a predicate IRI. Recorded so
            the caller can degrade gracefully; never produces a triple.
        parse_failed: ``True`` when ``sqlglot`` could not parse the SQL (or
            returned an empty/None tree). In that case ``triples`` and
            ``unmapped`` are both empty.
    """

    triples: List[Tuple[Optional[str], str]] = field(default_factory=list)
    unmapped: List[str] = field(default_factory=list)
    parse_failed: bool = False


def _alias_to_table(tree: exp.Expression) -> Dict[str, str]:
    """Map each table alias (and bare table name) to its real table name.

    Copied from the metadata agent's grounding module. Example:
    ``FROM normalized.address a`` -> ``{"address": "address", "a": "address"}``.
    This lets a qualified column reference (``a.state_tc``) resolve back to the
    real table (``address``) so it is looked up under the correct key.

    Args:
        tree: The parsed ``sqlglot`` expression tree.

    Returns:
        A mapping from lowercased alias/table token to the lowercased real
        table name.
    """
    mapping: Dict[str, str] = {}
    for tbl in tree.find_all(exp.Table):
        # ``tbl.name`` is empty for subquery-derived tables; skip those.
        if not tbl.name:
            continue
        real: str = tbl.name.lower()
        mapping[real] = real
        alias: str = tbl.alias_or_name
        if alias:
            mapping[alias.lower()] = real
    return mapping


def sql_to_triples(sql: str, *, idx: MappingIndex, dialect: str) -> BridgeResult:
    """Map ``sql`` into ontology ``(subject_class_iri | None, predicate_iri)`` pairs.

    Parses the SQL AST with ``sqlglot``, resolves each qualified column back to
    its real table via the alias map, and looks the ``"table.column"`` key up in
    ``idx.pred_by_column``. Columns inside ``JOIN ... ON`` predicates are plain
    ``exp.Column`` nodes and are walked identically, so a join key that maps to
    an object property (e.g. ``address.party_id`` -> ``.../hasParty``) naturally
    yields that triple.

    SELECT-output aliases and CTE names are skipped the same way the reference
    grounding module does, so an aggregate alias re-referenced in
    ``ORDER BY``/``HAVING``/``GROUP BY`` is not mistaken for a schema column.

    UNQUALIFIED columns resolve against the sole physical table when the query has
    exactly one in scope — the same thing SQL itself does, and the shape models
    write for single-table queries. With two or more tables in scope a bare column
    is ambiguous and stays ``unmapped``, because guessing its owner could ground
    the wrong subject class.

    Args:
        sql: The generated SQL statement.
        idx: The inverse Ontop mapping index (lowercased ``table``/
            ``table.column`` keys -> verbatim IRIs).
        dialect: ``sqlglot`` dialect identifier (e.g. ``"athena"``).

    Returns:
        A :class:`BridgeResult`. On any parse failure it has
        ``parse_failed=True`` and empty ``triples``/``unmapped``.
    """
    # Parse defensively: a syntax error raises ParseError, but a malformed input
    # can also yield None — treat both as a parse failure (fail-soft, no triples).
    try:
        tree: Optional[exp.Expression] = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return BridgeResult(triples=[], unmapped=[], parse_failed=True)
    if tree is None:
        return BridgeResult(triples=[], unmapped=[], parse_failed=True)

    alias_map: Dict[str, str] = _alias_to_table(tree)

    # CTE names are query-local aliases, not physical tables — never resolve
    # their columns against the mapping index.
    cte_names: Set[str] = {
        node.alias.lower() for node in tree.find_all(exp.CTE) if node.alias
    }
    # Output aliases declared anywhere (``<expr> AS name``); a bare column
    # re-reference matching one of these is the alias, not a real column.
    output_aliases: Set[str] = {
        node.alias.lower() for node in tree.find_all(exp.Alias) if node.alias
    }
    # The distinct PHYSICAL tables in scope (CTE references excluded). When there
    # is exactly one, plain SQL semantics resolve an unqualified column against
    # it, so the bridge does the same — see the unqualified branch below.
    real_tables: Set[str] = {
        node.name.lower() for node in tree.find_all(exp.Table)
        if node.name and node.name.lower() not in cte_names
    }
    sole_table: Optional[str] = (
        next(iter(real_tables)) if len(real_tables) == 1 else None
    )

    triples: List[Tuple[Optional[str], str]] = []
    unmapped: List[str] = []
    # Track emitted triples to de-dupe while preserving first-seen order.
    seen_triples: Set[Tuple[Optional[str], str]] = set()

    for node in tree.walk():
        if not isinstance(node, exp.Column):
            continue
        col: str = node.name.lower() if node.name else ""
        if not col or col == "*":
            # ``SELECT *`` and empty column names carry no schema reference.
            continue
        tbl_qualifier: str = node.table  # alias/table prefix, '' if unqualified

        if not tbl_qualifier:
            # An unqualified reference matching a SELECT output alias (e.g.
            # ``ORDER BY party_count``) is the computed alias, not a column.
            if col in output_aliases:
                continue
            # A single-table query needs no alias, and SQL resolves a bare column
            # against the only table in scope — so we do too. Without this, valid
            # single-table SQL (``SELECT COUNT(party_id) FROM normalized.party``)
            # produced zero triples, the grounding gate flagged CORRECT columns as
            # unverifiable, and the graph abstained holding a good query.
            # With 2+ tables in scope the reference is genuinely ambiguous:
            # guessing a table could ground the WRONG subject class, so it stays
            # unmapped and is routed back for qualification.
            if sole_table is None:
                unmapped.append(col)
                continue
            real_table: str = sole_table
        else:
            qualifier_key: str = tbl_qualifier.lower()
            # A qualifier that names a CTE is query-local; its columns aren't
            # physical schema columns, so skip it entirely.
            if qualifier_key in cte_names:
                continue
            # Resolve alias -> real table; fall back to the qualifier itself when
            # it is already a real table name not seen in the alias map.
            real_table = alias_map.get(qualifier_key, qualifier_key)

        column_key: str = f"{real_table}.{col}"

        predicate_iri: Optional[str] = idx.pred_by_column.get(column_key)
        if predicate_iri is None:
            # Fail-soft: no mapping for this column -> record the bare column
            # name, emit no triple (never a false ontology reference).
            unmapped.append(col)
            continue

        # Subject class IRI may be None when the table has no class mapping.
        subject_class_iri: Optional[str] = idx.class_by_table.get(real_table)
        triple: Tuple[Optional[str], str] = (subject_class_iri, predicate_iri)
        if triple not in seen_triples:
            seen_triples.add(triple)
            triples.append(triple)

    return BridgeResult(triples=triples, unmapped=unmapped, parse_failed=False)
