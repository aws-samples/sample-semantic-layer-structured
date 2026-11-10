"""Ontop mapping vocabulary + N-Quads emit/parse for the thin VKG builder.

The thin Virtual Knowledge Graph stores its Ontop mappings as two kinds of
triples:

    <classIRI>    vkg:mapsToTable  "table"
    <propertyIRI> vkg:mapsToColumn "table.column"

This module owns the vocabulary constants (copied verbatim from the
``ontology_agent`` package's ``VIRTUAL_KG_VOCAB`` value — copy-and-own, NOT an
import) plus small helpers to *emit* a single mapping triple and to *parse*
mapping (and, for completeness, ``rdfs:comment``) triples back out of N-Quads
text.

Copy-and-own: this module deliberately imports only the standard library. It
must not depend on ``agents.shared`` or any sibling agent package (enforced by
``tests/unit/test_hybrid_isolation.py``).
"""

import re
from typing import Dict, Sequence, Tuple

# Copied verbatim from agents/ontology_agent/prompt_builder.py::VIRTUAL_KG_VOCAB.
# This is the namespace prefix (trailing slash included) for the VKG vocabulary
# terms such as ``mapsToTable`` and ``mapsToColumn``.
VIRTUAL_KG_VOCAB: str = "https://semantic-layer.aws/virtual-kg/"

# Full angle-bracketed predicate IRIs used in the N-Quads text. Copied in shape
# from ontology_agent/main.py (_MAPS_TO_COLUMN_PRED / _MAPS_TO_TABLE_PRED).
MAPS_TO_COLUMN_PRED: str = f"<{VIRTUAL_KG_VOCAB}mapsToColumn>"
MAPS_TO_TABLE_PRED: str = f"<{VIRTUAL_KG_VOCAB}mapsToTable>"

# Verified primary key for a class. The translator reads this to build the OBDA
# subject template, which Ontop treats as ROW IDENTITY — every property access
# joins on it. Absent this triple the translator guesses by column name, and a
# guess that lands on a value column makes each access a self-join on a non-key
# column, silently returning wrong rows. Emit ONLY a key verified by measurement:
# its presence also licenses Ontop to drop de-duplication.
HAS_PRIMARY_KEY_PRED: str = f"<{VIRTUAL_KG_VOCAB}hasPrimaryKey>"

# rdfs:comment predicate — retained for the parse helper's completeness only.
# The thin VKG builder MUST NOT emit this predicate (prose lives in the KB).
RDFS_COMMENT_PRED: str = "<http://www.w3.org/2000/01/rdf-schema#comment>"


def emit_maps_to_table(*, class_iri: str, table: str, graph_iri: str) -> str:
    """Render a single ``mapsToTable`` N-Quad line.

    Args:
        class_iri: The ontology class IRI (subject), rendered verbatim.
        table: The physical table name stored as the literal object.
        graph_iri: The named-graph IRI placed in the quad's 4th position.

    Returns:
        A single N-Quad line terminated with `` .`` (no trailing newline).
    """
    return (
        f'<{class_iri}> {MAPS_TO_TABLE_PRED} "{table}" <{graph_iri}> .'
    )


def emit_maps_to_column(*, property_iri: str, table_column: str, graph_iri: str) -> str:
    """Render a single ``mapsToColumn`` N-Quad line.

    Args:
        property_iri: The ontology datatype-property IRI (subject), verbatim.
        table_column: The ``"table.column"`` physical reference literal.
        graph_iri: The named-graph IRI placed in the quad's 4th position.

    Returns:
        A single N-Quad line terminated with `` .`` (no trailing newline).
    """
    return (
        f'<{property_iri}> {MAPS_TO_COLUMN_PRED} "{table_column}" <{graph_iri}> .'
    )


def emit_has_primary_key(
    *, class_iri: str, key_columns: Sequence[str], table: str, graph_iri: str
) -> str:
    """Render a single ``hasPrimaryKey`` N-Quad line for a VERIFIED key.

    Columns are written in the dotted ``table.column`` form (comma-separated for a
    composite key) so the literal matches ``mapsToColumn``'s shape, which is what
    the translator's key parser accepts.

    Args:
        class_iri: The ontology class IRI (subject), rendered verbatim.
        key_columns: The verified key's bare column names, in key order. Order is
            preserved because it determines the subject template's column order,
            which must be stable across rebuilds — a reordered key would change
            every subject IRI in the layer.
        table: Physical table name used to qualify each column.
        graph_iri: The named-graph IRI placed in the quad's 4th position.

    Returns:
        A single N-Quad line terminated with `` .`` (no trailing newline).

    Raises:
        ValueError: ``key_columns`` is empty. A class with no verified key must
            have NO triple at all — an empty literal would parse to zero columns
            while still looking like a declaration, so the translator could no
            longer tell a measured key from a guess.
    """
    if not key_columns:
        raise ValueError(
            f"refusing to emit an empty hasPrimaryKey for {class_iri}: a class "
            f"with no verified key must carry no key triple at all."
        )
    literal = ", ".join(f"{table}.{column}" for column in key_columns)
    return f'<{class_iri}> {HAS_PRIMARY_KEY_PRED} "{literal}" <{graph_iri}> .'


def parse_nquad_mappings(
    nquad_content: str,
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """Parse mapping (and comment) triples from N-Quads text.

    Copies the parsing shape of ``ontology_agent.main._parse_nquad_metadata``:
    split each terminated line on whitespace, keep the subject + predicate, and
    reconstruct the (possibly space-containing) object literal by rejoining the
    remainder. Comment parsing is retained for completeness; callers that only
    need the mappings can ignore the first element.

    Args:
        nquad_content: Raw N-Quads text (one triple/quad per line).

    Returns:
        A ``(comments, maps_to_column, maps_to_table)`` tuple, each a
        ``Dict[subject_iri, literal_value]``.
    """
    comments: Dict[str, str] = {}
    maps_to_column: Dict[str, str] = {}
    maps_to_table: Dict[str, str] = {}

    for raw_line in nquad_content.splitlines():
        line = raw_line.strip()
        if not line or not line.endswith("."):
            continue
        # Drop the terminating '.' then split off subject + predicate; the
        # object literal may contain spaces so it is rejoined from the rest.
        parts = line[:-1].strip().split(" ", 3)
        if len(parts) < 3:
            continue
        subj, pred = parts[0], parts[1]
        obj = " ".join(parts[2:])

        if pred == RDFS_COMMENT_PRED:
            lit = re.match(r'"(.*)"', obj)
            if lit:
                comments[subj] = lit.group(1)
        elif pred == MAPS_TO_COLUMN_PRED:
            lit = re.match(r'"(.*)"', obj)
            if lit:
                maps_to_column[subj] = lit.group(1)
        elif pred == MAPS_TO_TABLE_PRED:
            lit = re.match(r'"(.*)"', obj)
            if lit:
                maps_to_table[subj] = lit.group(1)

    return comments, maps_to_column, maps_to_table
