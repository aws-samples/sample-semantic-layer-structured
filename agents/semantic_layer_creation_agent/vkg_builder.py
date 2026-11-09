"""Thin structural VKG builder for the Hybrid creation agent.

Emits the ontology backbone (``owl:Class`` + ``owl:DatatypeProperty`` structure),
the Ontop mappings (``vkg:mapsToTable`` / ``vkg:mapsToColumn``), and — when the
caller supplies them — a ONE-LINE ``rdfs:comment`` per class and property.

The verbose prose still stays out: only a single capped description line is
emitted, never the KB document's Business Purpose / Reference Tables / Common
Query Patterns / Sample Data / Notes sections. A thin VKG is still the point (a
smaller graph verifies faster, and the query path's CONSTRUCT sweeps every
predicate on a candidate class into the agent's ontology slice), but a graph with
NO descriptive text at all made the admin viewer's Description column render an
em-dash for every column — see ``frontend/src/components/OntologyEditor.jsx``,
which reads ``rdfs:comment`` for exactly that cell. ``kb_descriptions`` harvests
those lines out of the KB markdown so the description has a single source.

Copy-and-own: structural-emit and mapping logic are copied in shape from
``ontology_agent`` (prompt_builder.py's owl:Class / owl:DatatypeProperty N-Quads
example). Imports are limited to the standard library + this package's
``ontop_mappings`` module — no ``agents.shared`` or sibling-agent imports
(enforced by ``tests/unit/test_hybrid_isolation.py``).
"""

import re
from typing import Dict, List, Optional

from .ontop_mappings import (
    emit_has_primary_key,
    emit_maps_to_column,
    emit_maps_to_table,
)

# Characters SPARQL's IRIREF production forbids inside <...>: the explicit set
# plus every code point <= 0x20 (so SPACE and every control character). N-Quads'
# grammar is laxer, which is exactly the trap this module has to close — see
# :func:`iri_local_name`.
_IRI_ILLEGAL = re.compile(r'[\x00-\x20<>"{}|^`\\]')

# Full RDF / OWL / RDFS IRIs (angle-bracketed) used in N-Quads structural lines.
_RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
_OWL_CLASS = "<http://www.w3.org/2002/07/owl#Class>"
_OWL_DATATYPE_PROPERTY = "<http://www.w3.org/2002/07/owl#DatatypeProperty>"
_OWL_OBJECT_PROPERTY = "<http://www.w3.org/2002/07/owl#ObjectProperty>"
_RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
_RDFS_COMMENT = "<http://www.w3.org/2000/01/rdf-schema#comment>"
_RDFS_DOMAIN = "<http://www.w3.org/2000/01/rdf-schema#domain>"
_RDFS_RANGE = "<http://www.w3.org/2000/01/rdf-schema#range>"
_RDFS_SUBCLASS_OF = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"
_XSD_STRING = "<http://www.w3.org/2001/XMLSchema#string>"

# Glue/Hive column type -> XSD datatype local name. Ontop needs REAL datatypes to
# reformulate SPARQL into correct SQL: with everything typed xsd:string it emits
# string comparisons and cannot push down numeric aggregation or date ranges.
# Keys are the bare (de-parameterized, lowercased) Glue type names.
_GLUE_TO_XSD: Dict[str, str] = {
    "boolean": "boolean",
    "tinyint": "integer",
    "smallint": "integer",
    "int": "integer",
    "integer": "integer",
    "bigint": "long",
    "long": "long",
    "float": "double",
    "real": "double",
    "double": "double",
    "decimal": "decimal",
    "numeric": "decimal",
    "date": "date",
    "timestamp": "dateTime",
    "string": "string",
    "varchar": "string",
    "char": "string",
    "binary": "string",
}


def iri_local_name(text: str) -> str:
    """Turn a physical schema name into an IRI local name SPARQL will accept.

    Class and property IRIs are minted from Glue table / column names, which are
    free-form: ``bird_california_schools.frpm`` alone carries ``academic year``,
    ``county code`` and ``school name``. Interpolating those raw produces
    ``<https://semantic-layer.aws/virtual-kg/frpm/academic year>``, which the
    N-Quads grammar accepts (its IRI production is just ``<[^>]+>``) but SPARQL's
    IRIREF production does NOT — it excludes every code point <= 0x20. The
    gateway's ``INSERT DATA`` then dies with a MalformedQueryException whose
    column number points at the vocabulary host, giving no hint that a column
    name is at fault, and the whole build is discarded at the last step.

    Runs of illegal characters collapse into a single camel-case hump so the
    result stays readable and stable (``"academic year"`` -> ``"academicYear"``,
    matching what the VKG path's model-written IRIs already look like). The
    ORIGINAL name is never lost: it stays in ``rdfs:label`` and, crucially, in the
    ``vkg:mapsToColumn`` literal, which is what the OBDA translator reads to build
    SQL — so sanitizing the IRI cannot change which physical column is queried.

    Parameters:
        text: The raw table or column name from the Glue catalog.
    Returns:
        str: The sanitized local name.
    Raises:
        ValueError: ``text`` is blank, or consists ENTIRELY of characters that
            cannot appear in an IRI, which would collapse to an empty local name
            and silently merge two distinct columns into one property IRI.
    """
    if not text or not text.strip():
        raise ValueError("cannot mint an IRI local name from a blank name")
    # Split on runs of illegal characters, then camel-case the joins. The first
    # segment keeps its original case so table/column casing is preserved.
    segments = [segment for segment in _IRI_ILLEGAL.split(text.strip()) if segment]
    if not segments:
        raise ValueError(
            f"name {text!r} contains no characters that are legal in an IRI, so it "
            f"would collapse to an empty local name and could collide with another "
            f"column's property IRI"
        )
    head, *rest = segments
    return head + "".join(segment[:1].upper() + segment[1:] for segment in rest)


def nquads_literal(text: str) -> str:
    """Escape ``text`` into a quoted N-Quads literal.

    Descriptions are free prose: they carry double quotes, backslashes and — if a
    caller ever passes an un-flattened fragment — newlines. All three break the
    one-triple-per-line N-Quads grammar if emitted raw, and the admin viewer's
    line-oriented regex parser would drop the whole triple. The escape set here
    is exactly the one the viewer's ``getLiteral`` un-escapes.

    Parameters:
        text: The literal's lexical value.
    Returns:
        str: The literal including its surrounding double quotes.
    """
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def glue_type_to_xsd(glue_type: str) -> str:
    """Map a Glue/Hive column type to its angle-bracketed XSD datatype IRI.

    Parameters:
        glue_type: The Glue column type as reported by the Data Catalog, e.g.
            "bigint", "decimal(10,2)", "varchar(255)". Case-insensitive.
    Returns:
        str: The XSD datatype IRI in angle brackets, ready for an N-Quads object
            position (e.g. "<http://www.w3.org/2001/XMLSchema#integer>").

    Unknown or complex types (array/map/struct) fall back to xsd:string. That is
    the safe direction: Ontop treats an over-narrow numeric range as a hard cast
    error, whereas a string range merely forgoes an optimization.
    """
    # Glue reports parameterized types like "decimal(10,2)" / "varchar(255)";
    # only the head matters for datatype selection.
    bare = (glue_type or "").strip().lower().split("(", 1)[0].strip()
    local = _GLUE_TO_XSD.get(bare, "string")
    return f"<http://www.w3.org/2001/XMLSchema#{local}>"


def build_thin_vkg_nquads(
    *,
    table: str,
    columns: List[Dict[str, object]],
    class_iri: str,
    graph_iri: Optional[str] = None,
    subclass_of_iri: Optional[str] = None,
    joins: Optional[List[Dict[str, str]]] = None,
    key_columns: Optional[List[str]] = None,
    class_comment: Optional[str] = None,
    column_comments: Optional[Dict[str, str]] = None,
) -> str:
    """Build the thin structural VKG N-Quads for one table.

    Emits the class backbone, one datatype property per column, the Ontop mapping
    triples, and — only for the subjects a description was supplied for — a single
    ``rdfs:comment`` line. Prose SECTIONS are still never emitted; see the module
    docstring for why one line is the budget.

    Args:
        table: Physical table name; used verbatim in the mapping literals.
        columns: Column dicts, each requiring a ``name`` key. An optional
            ``type`` key carries the Glue column type and selects the
            ``rdfs:range`` datatype; when absent the range is ``xsd:string``.
            An optional ``comment`` key carries the column's catalog comment and
            is used as the ``rdfs:comment`` fallback when ``column_comments`` has
            no entry for the column.
        class_iri: The ontology class IRI this table maps to.
        graph_iri: Named-graph IRI for the 4th quad position. Defaults to
            ``class_iri`` when omitted.
        subclass_of_iri: Optional parent class IRI; when given, an
            ``rdfs:subClassOf`` structural triple is emitted (trivial hook).
        joins: Optional list of join hooks, each
            ``{"property": iri, "target_column": "t.c", "range": target_class_iri,
            "label": short_name}``. Every hook emits an ``owl:ObjectProperty``
            with ``rdfs:label`` / ``rdfs:domain`` / ``rdfs:range`` plus its
            ``vkg:mapsToColumn`` mapping. An optional ``comment`` key adds the
            edge's ``rdfs:comment`` (the viewer's Relationships Description cell).
            ``target_column`` is the dotted ``table.column`` of the REFERENCING
            (source) column — the same shape a datatype property maps to, since
            that is the column the OBDA template reads. ``range`` is required;
            ``label`` defaults to the property IRI's local name.
        key_columns: The table's VERIFIED primary-key columns, in key order. When
            given, a ``vkg:hasPrimaryKey`` triple is emitted and the translator
            uses it as the OBDA subject template (= row identity) instead of
            guessing by column name. Pass ``None`` when uniqueness was not
            measured — an unverified key is worse than none, since its presence
            also licenses Ontop to skip de-duplication.
        class_comment: One-line description for the class (the viewer's class
            description). Omitted from the output when empty.
        column_comments: Column name -> one-line description, keyed
            CASE-INSENSITIVELY (lookups lowercase the Glue column name) because
            the descriptions are harvested from prose written by a model, which
            does not reliably reproduce catalog column-name case. Takes precedence
            over a column dict's own ``comment``.

    Returns:
        The thin VKG as N-Quads text (one triple per line, newline-joined).

    Raises:
        ValueError: If a join hook carries no ``range`` (see ``joins`` above), or
            if a column dict is missing its required ``name`` key —
            fail loud rather than emitting a malformed property IRI. Also raised
            when ``key_columns`` names a column absent from ``columns``, since
            the subject template would then reference a column the mapping never
            declares and Ontop would fail with COLUMN_NOT_FOUND.
    """
    graph = graph_iri if graph_iri is not None else class_iri
    lines: List[str] = []
    # Descriptions are harvested from model-written prose, so match column names
    # case-insensitively against the catalog's casing (see column_comments docs).
    comments = {
        str(name).lower(): text
        for name, text in (column_comments or {}).items()
        if text
    }

    # --- Class backbone (structural + one description line) ---------------
    lines.append(f"<{class_iri}> {_RDF_TYPE} {_OWL_CLASS} <{graph}> .")
    # rdfs:label is a short structural token (the class local name), not prose.
    class_label = class_iri.rstrip("/").rsplit("/", 1)[-1]
    lines.append(f'<{class_iri}> {_RDFS_LABEL} "{class_label}" <{graph}> .')
    if class_comment:
        lines.append(
            f"<{class_iri}> {_RDFS_COMMENT} {nquads_literal(class_comment)} "
            f"<{graph}> ."
        )
    if subclass_of_iri is not None:
        lines.append(
            f"<{class_iri}> {_RDFS_SUBCLASS_OF} <{subclass_of_iri}> <{graph}> ."
        )
    # Ontop mapping: class -> physical table.
    lines.append(emit_maps_to_table(class_iri=class_iri, table=table, graph_iri=graph))

    # Verified primary key -> the OBDA subject template (row identity). Validated
    # against the emitted column set: a key naming an unmapped column would put a
    # column in the subject template that the mapping never declares.
    if key_columns:
        emitted = {str(column.get("name")) for column in columns if column.get("name")}
        missing = [column for column in key_columns if column not in emitted]
        if missing:
            raise ValueError(
                f"key_columns {missing!r} for {class_iri} are not among the emitted "
                f"columns {sorted(emitted)!r}. The subject template would reference "
                f"a column the mapping does not declare, and Ontop would fail with "
                f"COLUMN_NOT_FOUND at query time."
            )
        lines.append(
            emit_has_primary_key(
                class_iri=class_iri,
                key_columns=key_columns,
                table=table,
                graph_iri=graph,
            )
        )

    # --- One datatype property per column (structural + mapping) ----------
    # Sanitized local name -> raw column name, so two columns that sanitize to the
    # same IRI are caught instead of silently sharing one property.
    minted: Dict[str, str] = {}
    for column in columns:
        name = column.get("name")
        if not name:
            raise ValueError(f"Column dict missing required 'name' key: {column!r}")
        # The IRI gets a SPARQL-legal local name; the raw column name survives in
        # rdfs:label and in the mapsToColumn literal below (see iri_local_name).
        local = iri_local_name(str(name))
        clash = minted.get(local)
        if clash is not None and clash != str(name):
            raise ValueError(
                f"columns {clash!r} and {name!r} of table {table!r} both sanitize to "
                f"the IRI local name {local!r}. They would share one property IRI, so "
                f"one column's mapping would overwrite the other's."
            )
        minted[local] = str(name)
        prop_iri = f"{class_iri}/{local}"
        lines.append(
            f"<{prop_iri}> {_RDF_TYPE} {_OWL_DATATYPE_PROPERTY} <{graph}> ."
        )
        lines.append(f'<{prop_iri}> {_RDFS_LABEL} "{name}" <{graph}> .')
        # A harvested description wins over the catalog's own comment: S3 Tables /
        # Iceberg catalogs almost never carry column comments, so the Glue value
        # is usually empty and only ever a fallback for a hand-annotated catalog.
        description = comments.get(str(name).lower()) or str(
            column.get("comment") or ""
        ).strip()
        if description:
            lines.append(
                f"<{prop_iri}> {_RDFS_COMMENT} {nquads_literal(description)} "
                f"<{graph}> ."
            )
        lines.append(f"<{prop_iri}> {_RDFS_DOMAIN} <{class_iri}> <{graph}> .")
        # Range comes from the REAL Glue type so Ontop can reformulate numeric
        # and date predicates; absent type -> xsd:string (see glue_type_to_xsd).
        column_type = column.get("type")
        range_iri = (
            glue_type_to_xsd(str(column_type)) if column_type else _XSD_STRING
        )
        lines.append(f"<{prop_iri}> {_RDFS_RANGE} {range_iri} <{graph}> .")
        lines.append(
            emit_maps_to_column(
                property_iri=prop_iri,
                table_column=f"{table}.{name}",
                graph_iri=graph,
            )
        )

    # --- Join hooks (owl:ObjectProperty + mapping, no prose) --------------
    for join in joins or []:
        prop = join["property"]
        target_column = join["target_column"]
        # rdfs:range is REQUIRED, not optional. It is the target CLASS IRI, and
        # both consumers need it: the OBDA generator templates the FK object as
        # <range/{column}> so Ontop can unify it with the target's subject (a
        # range-less property becomes a bare literal that never joins), and the
        # graph viewer reads it as the relationship's "To Entity" column. A
        # range-less ObjectProperty is therefore an edge that is neither
        # queryable nor displayable — fail loud instead of emitting one.
        range_iri = join.get("range")
        if not range_iri:
            raise ValueError(
                f"join hook for {prop} has no 'range' (target class IRI). Without "
                f"it the OBDA mapping emits the FK as a literal that cannot unify "
                f"with the target class's subject template, so the join silently "
                f"reformulates to zero rows, and the graph viewer shows no target "
                f"entity."
            )
        lines.append(
            f"<{prop}> {_RDF_TYPE} {_OWL_OBJECT_PROPERTY} <{graph}> ."
        )
        # A short structural token (the property local name), not prose — this is
        # what the graph viewer shows as the relationship name.
        label = join.get("label") or prop.rstrip("/").rsplit("/", 1)[-1]
        lines.append(f'<{prop}> {_RDFS_LABEL} "{label}" <{graph}> .')
        join_comment = join.get("comment")
        if join_comment:
            lines.append(
                f"<{prop}> {_RDFS_COMMENT} {nquads_literal(join_comment)} "
                f"<{graph}> ."
            )
        lines.append(f"<{prop}> {_RDFS_DOMAIN} <{class_iri}> <{graph}> .")
        lines.append(f"<{prop}> {_RDFS_RANGE} <{range_iri}> <{graph}> .")
        lines.append(
            emit_maps_to_column(
                property_iri=prop, table_column=target_column, graph_iri=graph
            )
        )

    return "\n".join(lines) + "\n"
