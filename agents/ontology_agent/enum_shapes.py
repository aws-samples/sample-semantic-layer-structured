"""Pure, deterministic helpers for build-time SHACL enum-shape emission."""
from __future__ import annotations
import logging
import re
from typing import Dict, List

from rdflib import Dataset, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import Namespace, OWL, RDF, RDFS, XSD

logger = logging.getLogger(__name__)

# SHACL vocabulary. Use ``SH["in"]`` rather than ``SH.in`` because ``in`` is a
# reserved Python keyword and attribute access would be a syntax error.
SH = Namespace("http://www.w3.org/ns/shacl#")

MAX_ENUM_CARDINALITY = 25
MAX_ENUM_TOKEN_LEN = 40

# Predicate that ties an OWL DatatypeProperty back to its source "table.column".
MAPS_TO_COLUMN = URIRef("https://semantic-layer.aws/virtual-kg/mapsToColumn")


def is_enum(values: List[str], *, truncated: bool) -> bool:
    """True when ``values`` is a closed, short categorical vocabulary.

    Args:
        values: distinct column values returned by the probe.
        truncated: True if the probe hit its LIMIT (so the set is incomplete).
    Returns:
        True only when complete, non-empty, within the cardinality cap, and every
        value is a short non-empty token.
    """
    if truncated or not values:
        return False
    if len(values) > MAX_ENUM_CARDINALITY:
        return False
    return all(v and len(v) <= MAX_ENUM_TOKEN_LEN for v in values)


def extract_enum_candidates(nquads_text: str) -> List[Dict[str, str]]:
    """Find string-valued datatype properties that map to a source column.

    Parses the Phase-1 ontology N-Quads and selects every subject that is an
    ``owl:DatatypeProperty`` whose ``rdfs:range`` is ``xsd:string`` and which
    carries a ``vkg:mapsToColumn`` literal of the form ``"table.column"``. These
    are the columns worth probing for a closed categorical vocabulary.

    Args:
        nquads_text: The raw N-Quads serialization of the Phase-1 ontology.
    Returns:
        A list of dicts (sorted by ``prop_iri`` for determinism), each with keys
        ``prop_iri``, ``class_iri`` (from ``rdfs:domain``), ``column``, ``table``,
        and ``graph`` (the named-graph context IRI of the quads). Returns ``[]``
        if the input cannot be parsed or no candidate qualifies.
    """
    # Parse line-by-line and SKIP malformed lines, rather than parsing the whole
    # block atomically. Phase 2 occasionally emits a syntactically-invalid IRI
    # (e.g. an FK ObjectProperty IRI with literal spaces/parens like
    # ``.../Holding/hasHolding(self pk; bridge to party via coverage)``). Parsed
    # atomically, a single such line makes rdflib reject the ENTIRE block, which
    # would fail-soft this function to ``[]`` and silently suppress all enum shapes
    # for that table — exactly the columns (e.g. holding_status) we most need.
    # Isolating the parse per line means one bad triple can't nuke a whole table's
    # enum discovery.
    dataset = Dataset()
    skipped = 0
    for line in nquads_text.splitlines():
        if not line.strip():
            continue
        try:
            dataset.parse(data=line, format="nquads")
        except Exception:  # noqa: BLE001 — skip the bad line, keep the rest
            skipped += 1
            continue
    if skipped:
        logger.warning(
            "extract_enum_candidates: skipped %d malformed N-Quad line(s)", skipped
        )

    candidates: List[Dict[str, str]] = []

    # Iterate every subject typed as an OWL DatatypeProperty. On a Dataset,
    # quads() spans every named graph and exposes the quad context (4th term),
    # so this is how we recover the named-graph IRI. (Dataset.objects() would
    # only query the default graph and miss triples living in a named graph.)
    for subj, _pred, _obj, ctx in dataset.quads(
        (None, RDF.type, OWL.DatatypeProperty, None)
    ):
        # ctx is the rdflib Graph for the quad's named graph; its identifier is
        # the graph IRI we want to surface. Scope all further lookups to ctx so
        # we only consider triples asserted in the same named graph.
        graph = ctx if hasattr(ctx, "objects") else dataset.graph(ctx)

        # Require rdfs:range == xsd:string within the same graph.
        if XSD.string not in set(graph.objects(subj, RDFS.range)):
            continue

        # Require a vkg:mapsToColumn literal shaped "table.column".
        column_literal = next(
            (
                obj
                for obj in graph.objects(subj, MAPS_TO_COLUMN)
                if isinstance(obj, Literal) and "." in str(obj)
            ),
            None,
        )
        if column_literal is None:
            continue
        table, column = str(column_literal).split(".", 1)

        # Require an rdfs:domain (the owning class); skip if absent.
        domain = next(iter(graph.objects(subj, RDFS.domain)), None)
        if domain is None:
            continue

        candidates.append(
            {
                "prop_iri": str(subj),
                "class_iri": str(domain),
                "column": column,
                "table": table,
                "graph": str(graph.identifier),
            }
        )

    # Sort by prop_iri for deterministic output ordering.
    return sorted(candidates, key=lambda c: c["prop_iri"])


def build_enum_shape_nquads(
    *, class_iri: str, prop_iri: str, values: List[str], graph: str
) -> str:
    """Return N-Quads (in named graph ``graph``) for a SHACL sh:in shape, or "".

    Emits NodeShape -> sh:targetClass -> sh:property -> sh:path + sh:in(list).
    The shape node and the RDF-list head node are minted as IRIs under
    ``{prop_iri}Shape`` (NOT bare blank nodes) so that concatenating many tables'
    N-Quads into one Neptune graph never collides same-named columns across
    tables — each table's column has a distinct ``prop_iri`` and therefore a
    distinct shape/list head IRI. Returns "" when ``values`` is empty or any
    value contains a char that can't be safely emitted as an N-Quads literal
    (``"``, ``\\``, newline, carriage return) — fail loud by emitting nothing
    rather than producing malformed quads.

    The RDF list spine (``rdf:first``/``rdf:rest``/``rdf:nil``) is built with
    ``rdflib.collection.Collection`` directly into the named graph — never
    hand-written — so the spine is guaranteed correct and lands entirely inside
    ``graph``.

    Args:
        class_iri: the OWL class the shape targets (``sh:targetClass``).
        prop_iri: the DatatypeProperty IRI the enum constrains (``sh:path``).
        values: the verified closed value set; order is preserved in the list.
        graph: the named-graph IRI emitted as the 4th term of every quad.
    Returns:
        N-Quads text (one quad per line) or "" to skip emission.
    """
    # Chars that would break an N-Quads literal if emitted unescaped; we choose
    # to skip the whole shape rather than risk a malformed quad in Neptune.
    unsafe = ('"', "\\", "\n", "\r")
    if not values or any(any(c in v for c in unsafe) for v in values):
        return ""

    # A Dataset lets us add triples into a named graph and serialize the whole
    # thing as N-Quads with the graph IRI as the 4th term on every line.
    dataset = Dataset()
    named_graph = dataset.graph(URIRef(graph))

    # IRI nodes minted under {prop_iri}Shape — unique per property, so two
    # tables' identically-named columns never share a shape/list node.
    shape = URIRef(f"{prop_iri}Shape")
    property_shape = URIRef(f"{prop_iri}Shape/prop")
    list_head = URIRef(f"{prop_iri}Shape/list")

    named_graph.add((shape, RDF.type, SH.NodeShape))
    named_graph.add((shape, SH.targetClass, URIRef(class_iri)))
    named_graph.add((shape, SH.property, property_shape))
    named_graph.add((property_shape, SH.path, URIRef(prop_iri)))
    named_graph.add((property_shape, SH["in"], list_head))

    # Collection writes the rdf:first/rdf:rest/rdf:nil spine into named_graph,
    # rooted at the IRI list_head, preserving value order.
    Collection(named_graph, list_head, [Literal(v) for v in values])

    return dataset.serialize(format="nquads")


# Column-name suffixes/names that denote a categorical/coded field.
_CATEGORICAL_NAME_RE = re.compile(
    r"(_(status|type|code|category|state|kind|class|level|flag|tier|rating|"
    r"frequency|freq|mode|method|role|reason|indicator|ind)$)"
    r"|^(status|type|code|category|state|gender|kind|class|level|flag|tier|"
    r"rating|frequency|mode|method|role|reason|currency|country|region|"
    r"marital_status|relationship)$",
    re.IGNORECASE,
)
# A true closed vocabulary repeats HEAVILY: distinct is a small fraction of the
# sample. 0.2 means a value recurs on average >=5x before it counts as a real
# closed set. The name signal remains the primary, more reliable path.
_MAX_ENUM_DISTINCT_RATIO = 0.2


def is_enum_column(column: str, values: list, *, distinct_count: int,
                   sample_rows: int) -> bool:
    """True when a column is a genuine closed-enum, not merely low-cardinality.

    Precision layer applied AFTER is_enum passes on the values: requires a
    categorical SIGNAL — either a categorical-looking column name, OR a low
    distinct-to-sample ratio (a real enum repeats its values across rows). This
    stops small synthetic datasets from shaping free-text / identifier columns
    (full_name, govt_id, dates) that happen to have <=25 distinct values.

    Args:
        column: the source column name (from vkg:mapsToColumn's ``table.column``).
        values: the distinct values (already gate-checked by is_enum upstream).
        distinct_count: number of distinct values observed.
        sample_rows: number of rows the probe counted over (ratio denominator).
    Returns:
        True only when non-empty AND (categorical name OR low repeating ratio).
    """
    if not values:
        return False
    name_signal = bool(_CATEGORICAL_NAME_RE.search(column or ""))
    ratio_signal = (
        sample_rows >= 50
        and distinct_count / sample_rows <= _MAX_ENUM_DISTINCT_RATIO
    )
    return name_signal or ratio_signal
