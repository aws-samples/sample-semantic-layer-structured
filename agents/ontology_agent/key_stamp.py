"""Stamp verified ``vkg:hasPrimaryKey`` triples onto a consolidated ontology.

Where this sits
---------------
``key_probe`` MEASURES which columns are unique. This module decides which
columns are worth measuring, writes the result into the ontology graph, and is
called once at assembly time — after Phase 2, on the consolidated N-Quads, just
before the layer is marked ``completed``.

The key is stamped in deterministic Python rather than requested from the Phase 1
LLM prompt. A key is a measured fact about the data, and the model has no way to
measure it: asked for a primary key it would pattern-match on the column name,
which is exactly the heuristic this whole change exists to replace. Worse, a
model-emitted key would be indistinguishable in the graph from a verified one, so
the consumer could no longer tell a fact from a guess — and the consumer uses
that distinction to decide whether to emit a ``uniqueConstraint`` (a licence for
Ontop to drop de-duplication).

What gets stamped
-----------------
One triple per class whose key was verified::

    <classIRI> <vkg:hasPrimaryKey> "table.col"           <graph> .
    <classIRI> <vkg:hasPrimaryKey> "table.a, table.b"    <graph> .   (composite)

The literal is the dotted ``table.column`` form already used by ``mapsToColumn``,
comma-separated for a composite key, which is the shape
``OntologyMappings.declaredKeyColumnsFor`` parses on the Java side.

A class whose key cannot be verified gets NO triple. The consumer then falls back
to its name heuristic and logs loudly that row identity is a guess. That is a
deliberately visible degradation rather than a silent one.
"""

import logging
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .key_probe import RunProbe, discover_primary_key

logger = logging.getLogger(__name__)

# Vocabulary. Copied in shape from main.py's _MAPS_TO_* constants so this module
# stays importable without pulling main.py's boto3/Strands dependencies.
VIRTUAL_KG_VOCAB: str = "https://semantic-layer.aws/virtual-kg/"
MAPS_TO_TABLE_PRED: str = f"<{VIRTUAL_KG_VOCAB}mapsToTable>"
MAPS_TO_COLUMN_PRED: str = f"<{VIRTUAL_KG_VOCAB}mapsToColumn>"
HAS_PRIMARY_KEY_PRED: str = f"<{VIRTUAL_KG_VOCAB}hasPrimaryKey>"

# A probe-factory seam: given a database + catalog, return the RunProbe that
# queries it. Injected so tests never touch AWS, and so the caller owns the
# Athena workgroup / output-location resolution.
ProbeFactory = Callable[[str, Optional[str]], RunProbe]


class ClassMapping:
    """One class's physical binding, as read out of the ontology N-Quads.

    Attributes:
        class_iri: The owl:Class IRI (the subject a key is stamped on).
        database: Database segment of the ``mapsToTable`` literal, or ``None``
            when the literal was a bare table name.
        table: Bare table name.
        columns: Bare column names mapped by this class's properties, in the
            order encountered (which is catalog order, since the emitters walk
            the Glue column list).
        graph: The named-graph IRI from the quad's 4th position, so a stamped
            triple lands in the SAME graph — a triple in another graph would be
            invisible to the query side's graph-scoped CONSTRUCT.
    """

    def __init__(
        self,
        *,
        class_iri: str,
        database: Optional[str],
        table: str,
        columns: List[str],
        graph: Optional[str],
    ) -> None:
        """Store the parsed binding fields verbatim.

        Args:
            class_iri: The owl:Class IRI.
            database: Database name, or ``None`` if the literal had no dot.
            table: Bare table name.
            columns: Bare mapped column names in encounter order.
            graph: Named-graph IRI, or ``None`` when the line was a triple.
        """
        self.class_iri = class_iri
        self.database = database
        self.table = table
        self.columns = columns
        self.graph = graph


def _literal(obj_field: str) -> Optional[str]:
    """Extract a quoted literal's value from an N-Quad object field.

    Args:
        obj_field: The object position onward, e.g. ``'"db.t" <graph> .'``.

    Returns:
        The literal's inner text, or ``None`` when the field is not a literal
        (e.g. an IRI object).
    """
    match = re.match(r'"(.*?)"', obj_field)
    return match.group(1) if match else None


def _graph_of(obj_field: str) -> Optional[str]:
    """Extract the named-graph IRI that follows a literal object, if present.

    Args:
        obj_field: The object position onward, e.g. ``'"db.t" <graph> .'``.

    Returns:
        The graph IRI without angle brackets, or ``None`` for a plain triple.
    """
    match = re.match(r'".*?"\s+<([^>]+)>', obj_field)
    return match.group(1) if match else None


def parse_class_mappings(nquads: str) -> List[ClassMapping]:
    """Read each class's table + mapped columns + graph out of ontology N-Quads.

    A class's columns are the ``mapsToColumn`` values of the properties NESTED
    under its IRI (``<classIRI>/<prop>``) — the same nesting rule the Java
    consumer's ``sortedPropertyIrisForClass`` applies, so the column set probed
    here matches the column set the OBDA mapping will declare. Probing a column
    the mapping does not declare would verify a key Ontop cannot reference.

    Args:
        nquads: The consolidated ontology N-Quads text.

    Returns:
        One :class:`ClassMapping` per class that has a ``mapsToTable``, in
        sorted class-IRI order so a rebuild stamps in a stable order.
    """
    tables: Dict[str, str] = {}
    graphs: Dict[str, str] = {}
    # class IRI -> ordered, de-duplicated bare column names.
    columns: Dict[str, List[str]] = {}

    for raw_line in nquads.splitlines():
        line = raw_line.strip()
        if not line or not line.endswith("."):
            continue
        parts = line[:-1].strip().split(" ", 2)
        if len(parts) < 3:
            continue
        subject_field, predicate, obj_field = parts[0], parts[1], parts[2]
        if not (subject_field.startswith("<") and subject_field.endswith(">")):
            continue
        subject = subject_field[1:-1]

        if predicate == MAPS_TO_TABLE_PRED:
            value = _literal(obj_field)
            if value:
                tables[subject] = value
                graph = _graph_of(obj_field)
                if graph:
                    graphs[subject] = graph
        elif predicate == MAPS_TO_COLUMN_PRED:
            value = _literal(obj_field)
            if not value:
                continue
            # The owning class is the property IRI minus its last segment.
            if "/" not in subject:
                continue
            class_iri = subject.rsplit("/", 1)[0]
            bare = value.rsplit(".", 1)[-1]
            bucket = columns.setdefault(class_iri, [])
            # Two properties may map to the SAME column; probing it twice would
            # waste a full-table scan and duplicate an aggregate alias.
            if bare not in bucket:
                bucket.append(bare)

    mappings: List[ClassMapping] = []
    for class_iri in sorted(tables):
        literal = tables[class_iri]
        if "." in literal:
            database, table = literal.rsplit(".", 1)
        else:
            database, table = None, literal
        mappings.append(
            ClassMapping(
                class_iri=class_iri,
                database=database,
                table=table,
                columns=columns.get(class_iri, []),
                graph=graphs.get(class_iri),
            )
        )
    return mappings


def emit_has_primary_key(
    *, class_iri: str, table: str, key_columns: Sequence[str], graph: Optional[str]
) -> str:
    """Render one ``hasPrimaryKey`` N-Quad line for a verified key.

    Columns are written in the dotted ``table.column`` form (comma-separated for
    a composite) to match ``mapsToColumn``'s shape, which is what the Java
    consumer's key parser already accepts.

    Args:
        class_iri: The owl:Class IRI the key belongs to.
        table: Bare table name, used to build the dotted references.
        key_columns: The verified key's bare column names, in key order.
        graph: Named-graph IRI, or ``None`` to emit a plain triple.

    Returns:
        A single N-Quad/triple line terminated with `` .``.

    Raises:
        ValueError: ``key_columns`` is empty — an empty key literal would parse
            to no columns on the consumer side while still LOOKING like a
            declaration, which is worse than emitting nothing.
    """
    if not key_columns:
        raise ValueError(
            f"refusing to emit an empty hasPrimaryKey for {class_iri}: "
            f"a class with no verified key must have NO triple at all, so the "
            f"consumer can tell a guess from a fact."
        )
    literal = ", ".join(f"{table}.{column}" for column in key_columns)
    graph_field = f" <{graph}>" if graph else ""
    return f'<{class_iri}> {HAS_PRIMARY_KEY_PRED} "{literal}"{graph_field} .'


def stamp_primary_keys(
    *,
    nquads: str,
    probe_factory: ProbeFactory,
    catalog_for: Optional[Callable[[str], Optional[str]]] = None,
) -> Tuple[str, Dict[str, List[str]]]:
    """Append verified ``hasPrimaryKey`` triples to consolidated ontology N-Quads.

    Probes each mapped class's columns for uniqueness and stamps a key where one
    is verified. Classes already carrying a ``hasPrimaryKey`` are left untouched,
    so re-running assembly (or a delta build) neither re-probes nor duplicates.

    FAIL-SOFT PER CLASS, BY DESIGN: a probe that errors logs a warning and leaves
    that class unstamped. This does NOT contradict the repo's fail-loudly rule —
    the un-keyed outcome is the pre-existing behaviour and the consumer already
    warns loudly when it has to guess row identity. Aborting a whole 75-table
    layer build because one table's probe timed out would be strictly worse, and
    a wrong key (the alternative) is precisely the defect being fixed.

    Args:
        nquads: The consolidated ontology N-Quads text.
        probe_factory: Given ``(database, catalog)``, returns the ``RunProbe``
            that queries it.
        catalog_for: Optional ``database -> catalog`` resolver. When ``None``,
            the factory is called with a ``None`` catalog.

    Returns:
        ``(stamped_nquads, keys_by_class)`` — the input text with the new triples
        appended, and the verified key columns per class IRI (only classes that
        were stamped this call).
    """
    already_stamped = {
        line.strip().split(" ", 2)[0][1:-1]
        for line in nquads.splitlines()
        if HAS_PRIMARY_KEY_PRED in line and line.strip().startswith("<")
    }

    new_lines: List[str] = []
    keys_by_class: Dict[str, List[str]] = {}

    for mapping in parse_class_mappings(nquads):
        if mapping.class_iri in already_stamped:
            logger.info(
                "[KeyStamp] %s already declares a primary key — leaving as-is",
                mapping.class_iri,
            )
            continue
        if not mapping.columns:
            # No mapped columns: the consumer skips such a class entirely, so
            # there is nothing a key could identify.
            logger.warning(
                "[KeyStamp] %s maps table %s but no columns — cannot probe a key",
                mapping.class_iri, mapping.table,
            )
            continue
        if not mapping.database:
            logger.warning(
                "[KeyStamp] %s mapsToTable %r has no database segment — cannot "
                "build a qualified probe query; leaving unstamped",
                mapping.class_iri, mapping.table,
            )
            continue

        catalog = catalog_for(mapping.database) if catalog_for else None
        try:
            probe = probe_factory(mapping.database, catalog)
            key_columns = discover_primary_key(
                database=mapping.database,
                table=mapping.table,
                columns=mapping.columns,
                run_probe=probe,
            )
        except Exception as exc:  # noqa: BLE001 — per-class fail-soft; see docstring
            logger.warning(
                "[KeyStamp] key probe failed for %s.%s (%s) — leaving unstamped; "
                "the translator will fall back to its name heuristic",
                mapping.database, mapping.table, exc,
            )
            continue

        if not key_columns:
            logger.warning(
                "[KeyStamp] no unique column or column pair found in %s.%s — "
                "leaving unstamped; row identity will be a name guess",
                mapping.database, mapping.table,
            )
            continue

        new_lines.append(
            emit_has_primary_key(
                class_iri=mapping.class_iri,
                table=mapping.table,
                key_columns=key_columns,
                graph=mapping.graph,
            )
        )
        keys_by_class[mapping.class_iri] = key_columns
        logger.info(
            "[KeyStamp] verified key for %s.%s: %s",
            mapping.database, mapping.table, ", ".join(key_columns),
        )

    if not new_lines:
        return nquads, keys_by_class

    # Preserve the input's trailing-newline shape so the result stays a clean
    # concatenation of one quad per line.
    body = nquads if nquads.endswith("\n") else nquads + "\n"
    return body + "\n".join(new_lines) + "\n", keys_by_class
