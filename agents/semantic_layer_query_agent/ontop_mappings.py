"""Inverse index over the thin VKG's Ontop mapping triples.

The thin Virtual Knowledge Graph stores Ontop mappings in Neptune as two kinds
of triples:

    <classIRI>    vkg:mapsToTable  "table"
    <propertyIRI> vkg:mapsToColumn "table.column"

At query time the Hybrid query agent fetches those triples as an
``rdflib.Graph`` and needs the *inverse* lookup — given a physical
``table.column`` or ``table`` produced by generated SQL, find the ontology
predicate / class IRI it maps to. This module builds that inverse index.

Copy-and-own: this module deliberately imports only the standard library and
``rdflib``. It must not depend on ``agents.shared`` or any sibling agent
package (enforced by ``tests/unit/test_hybrid_isolation.py``).
"""

from dataclasses import dataclass
from typing import Dict

from rdflib import Graph, URIRef
from rdflib.term import Literal, Node

# The vocabulary namespace the mapping PREDICATES live in. Must stay identical to
# ``semantic_layer_creation_agent/ontop_mappings.py::VIRTUAL_KG_VOCAB`` (and to
# ``ontology_agent/prompt_builder.py``) — the creation agent writes
# ``<vocab>mapsToTable`` / ``<vocab>mapsToColumn`` and this package reads them
# back by full IRI, so a divergence silently yields an EMPTY inverse index and
# every query abstains as ungrounded. Copied, not imported (isolation rule).
VIRTUAL_KG_VOCAB: str = "https://semantic-layer.aws/virtual-kg/"


@dataclass
class MappingIndex:
    """Inverse lookup tables over the Ontop mapping triples.

    Attributes:
        class_by_table: Maps a lowercased physical table name to the ontology
            class IRI (verbatim) that ``vkg:mapsToTable`` associates with it.
        pred_by_column: Maps a lowercased ``"table.column"`` string to the
            ontology predicate IRI (verbatim) from ``vkg:mapsToColumn``.
        table_by_class: Inverse of ``class_by_table`` — maps the class IRI
            (verbatim) to the lowercased physical table name.
    """

    class_by_table: Dict[str, str]
    pred_by_column: Dict[str, str]
    table_by_class: Dict[str, str]


def _literal_text(*, obj: Node) -> str:
    """Return the string value of an object node, failing loud if not a literal.

    Args:
        obj: The object node of a mapping triple, expected to be an
            ``rdflib`` ``Literal`` carrying the physical table / column name.

    Returns:
        The literal's string value.

    Raises:
        TypeError: If ``obj`` is not an ``rdflib`` ``Literal`` — the mapping
            triples are malformed and we must not silently skip them.
    """
    if not isinstance(obj, Literal):
        raise TypeError(
            f"Expected a Literal object for an Ontop mapping triple, got "
            f"{type(obj).__name__}: {obj!r}"
        )
    return str(obj)


def build_inverse_index(graph: Graph, *, vocab: str) -> MappingIndex:
    """Build the inverse index of Ontop mapping triples in ``graph``.

    Walks the graph for ``<vocab>mapsToTable`` and ``<vocab>mapsToColumn``
    triples, matching predicates by full IRI. Object literal values (physical
    table names and ``"table.column"`` strings) are lowercased to form the
    lookup keys for case-insensitive matching downstream; subject IRIs are
    kept verbatim as the values.

    Args:
        graph: The ``rdflib.Graph`` holding the thin VKG's mapping triples.
        vocab: The vocabulary namespace prefix (including the trailing ``#``
            or ``/``) whose ``mapsToTable`` / ``mapsToColumn`` terms are used.

    Returns:
        A ``MappingIndex`` with ``class_by_table``, ``pred_by_column``, and
        ``table_by_class`` populated.
    """
    maps_to_table: URIRef = URIRef(vocab + "mapsToTable")
    maps_to_column: URIRef = URIRef(vocab + "mapsToColumn")

    class_by_table: Dict[str, str] = {}
    pred_by_column: Dict[str, str] = {}
    table_by_class: Dict[str, str] = {}

    # mapsToTable: subject is the class IRI, object literal is the table name.
    for subject, _predicate, obj in graph.triples((None, maps_to_table, None)):
        table_key: str = _literal_text(obj=obj).lower()
        class_iri: str = str(subject)
        class_by_table[table_key] = class_iri
        table_by_class[class_iri] = table_key

    # mapsToColumn: subject is the predicate IRI, object literal is "table.column".
    for subject, _predicate, obj in graph.triples((None, maps_to_column, None)):
        column_key: str = _literal_text(obj=obj).lower()
        predicate_iri: str = str(subject)
        pred_by_column[column_key] = predicate_iri

    return MappingIndex(
        class_by_table=class_by_table,
        pred_by_column=pred_by_column,
        table_by_class=table_by_class,
    )
