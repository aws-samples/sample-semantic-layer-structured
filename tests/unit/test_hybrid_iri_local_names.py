"""IRI local names minted from physical schema names must be SPARQL-legal.

A Hybrid build over ``bird_california_schools`` failed on 2026-09-04 at
``progressPercent: 95`` — after all three tables were documented, i.e. after the
expensive part. The thin VKG minted its property IRIs straight from the Glue
column names, so the payload contained::

    <https://semantic-layer.aws/virtual-kg/frpm/academic year>

N-Quads' IRI production is effectively ``<[^>]+>``, so both the producer's
pre-flight and the gateway's parser accepted it. SPARQL's IRIREF production
excludes every code point <= 0x20, so Neptune refused the resulting
``INSERT DATA``::

    MalformedQueryException: Lexical error at line 13, column 32.
    Encountered: "/" (47), after : "semantic-layer.aws"

That column number points at the vocabulary host, naming neither the offending
line nor the column at fault. The VKG path never hit this because its property
IRIs are written by a model that camel-cases by convention — a convention, not an
enforced invariant.

These tests pin the three properties that close the gap:

1. ``iri_local_name`` produces a SPARQL-legal local name and keeps the raw name
   reachable (``rdfs:label`` / ``vkg:mapsToColumn``), so sanitizing an IRI can
   never change which physical column the generated SQL reads;
2. a whole table's worth of spaced column names emits N-Quads that BOTH the
   pre-flight and the gateway grammar accept;
3. an IRI carrying an illegal character is now rejected by the pre-flight — the
   check that runs BEFORE anything is sent — rather than by Neptune.
"""

import importlib.util
import os

import pytest

from agents.semantic_layer_creation_agent.neptune_load import (
    _NQUAD_PATTERN,
    validate_nquads,
)
from agents.semantic_layer_creation_agent.vkg_builder import (
    build_thin_vkg_nquads,
    iri_local_name,
)

# Load lambda/neptune-tools/index.py under a UNIQUE module name — several Lambda
# handlers under test are named `index.py` and a plain `import index` would hand
# this file whichever one loaded first in a full-suite run.
_NEPTUNE_TOOLS_INDEX_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "lambda", "neptune-tools", "index.py"
    )
)
_spec = importlib.util.spec_from_file_location(
    "neptune_tools_index_iri_names", _NEPTUNE_TOOLS_INDEX_PATH
)
index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index)

VOCAB = "https://semantic-layer.aws/virtual-kg/"
GRAPH = "https://semantic-layer.aws/vkg/df3d87e6"

# The real column names of bird_california_schools.frpm that broke the build.
FRPM_COLUMNS = [
    {"name": "cdscode", "type": "string"},
    {"name": "academic year", "type": "string"},
    {"name": "county code", "type": "string"},
    {"name": "enrollment (k-12)", "type": "double"},
    {"name": "percent (%) eligible frpm (k-12)", "type": "double"},
]


# --------------------------------------------------------------------------- #
# 1. The sanitizer.                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("cdscode", "cdscode"),
        ("academic year", "academicYear"),
        ("county code", "countyCode"),
        # Underscores and hyphens are legal in an IRI and must survive verbatim:
        # they are how most catalogs already spell multi-word columns, and
        # rewriting them would change IRIs for every existing layer.
        ("academic_year", "academic_year"),
        ("free-lunch-count", "free-lunch-count"),
        # Parentheses and % are legal IRI characters; only the spaces collapse.
        ("enrollment (k-12)", "enrollment(k-12)"),
        ("percent (%) eligible frpm (k-12)", "percent(%)EligibleFrpm(k-12)"),
        # Runs of illegal characters collapse to a single hump.
        ("a   b", "aB"),
        # Tabs and control characters are illegal too, not just SPACE.
        ("a\tb", "aB"),
        # The explicitly-forbidden IRIREF set.
        ('a"b', "aB"),
        ("a{b}c", "aBC"),
        ("a|b", "aB"),
        ("a^b", "aB"),
        ("a\\b", "aB"),
        ("a<b>c", "aBC"),
    ],
)
def test_sanitizes_to_a_sparql_legal_local_name(raw: str, expected: str) -> None:
    """Illegal characters collapse into camel-case humps; legal ones survive."""
    assert iri_local_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", " \t ", "<>", '"{}|^`'])
def test_rejects_names_with_no_legal_characters(raw: str) -> None:
    """A name that would collapse to an empty local name must fail loudly.

    An empty local name would make two distinct columns share one property IRI,
    so the second column's mapping would overwrite the first's — a wrong graph
    that still loads, which is worse than a failed build.
    """
    with pytest.raises(ValueError):
        iri_local_name(raw)


def test_every_sanitized_name_is_accepted_in_an_iri() -> None:
    """No sanitizer output may contain a character SPARQL's IRIREF forbids."""
    for column in FRPM_COLUMNS:
        iri = f"<{VOCAB}frpm/{iri_local_name(str(column['name']))}>"
        line = f"{iri} <{VOCAB}mapsToColumn> \"x\" <{GRAPH}> ."
        assert _NQUAD_PATTERN.match(line), line
        assert index.NQUAD_PATTERN.match(line), line


# --------------------------------------------------------------------------- #
# 2. The emitted graph.                                                       #
# --------------------------------------------------------------------------- #
def test_spaced_columns_emit_a_loadable_graph() -> None:
    """The table that broke the live build now validates end to end."""
    nquads = build_thin_vkg_nquads(
        table="frpm",
        columns=FRPM_COLUMNS,
        class_iri=f"{VOCAB}frpm",
        graph_iri=GRAPH,
    )
    # The pre-flight is the gate the failed build sailed through; it must pass now.
    assert validate_nquads(nquads) == len(nquads.strip().split("\n"))
    # And no line may carry a raw spaced IRI any more.
    assert "/academic year>" not in nquads
    assert f"<{VOCAB}frpm/academicYear>" in nquads


def test_raw_column_name_survives_in_label_and_mapping() -> None:
    """Sanitizing the IRI must not change which physical column is queried.

    ``vkg:mapsToColumn`` is what the OBDA translator reads to build SQL, and
    ``rdfs:label`` is what the admin viewer displays — both must still carry
    ``academic year`` exactly as the catalog spells it.
    """
    nquads = build_thin_vkg_nquads(
        table="frpm",
        columns=[{"name": "academic year", "type": "string"}],
        class_iri=f"{VOCAB}frpm",
        graph_iri=GRAPH,
    )
    assert '"academic year"' in nquads
    assert '"frpm.academic year"' in nquads


def test_columns_colliding_after_sanitizing_fail_loudly() -> None:
    """Two columns that sanitize to one local name must not silently merge."""
    with pytest.raises(ValueError, match="sanitize to the IRI local name"):
        build_thin_vkg_nquads(
            table="frpm",
            columns=[{"name": "academic year"}, {"name": "academic  year"}],
            class_iri=f"{VOCAB}frpm",
            graph_iri=GRAPH,
        )


# --------------------------------------------------------------------------- #
# 3. The grammars.                                                            #
# --------------------------------------------------------------------------- #
BAD_IRIS = [
    f"<{VOCAB}frpm/academic year>",
    f"<{VOCAB}frpm/a\tb>",
    f'<{VOCAB}frpm/a"b>',
    f"<{VOCAB}frpm/a{{b}}>",
    f"<{VOCAB}frpm/a|b>",
    f"<{VOCAB}frpm/a^b>",
    f"<{VOCAB}frpm/a\\b>",
]


@pytest.mark.parametrize("bad", BAD_IRIS)
def test_preflight_rejects_iris_sparql_cannot_lex(bad: str) -> None:
    """The pre-flight, not Neptune, is where an unlexable IRI must be caught."""
    line = f'{bad} <{VOCAB}mapsToColumn> "x" <{GRAPH}> .'
    with pytest.raises(ValueError, match="REJECTED by the gateway's parser"):
        validate_nquads(line)


@pytest.mark.parametrize("bad", BAD_IRIS)
def test_gateway_grammar_matches_the_preflight(bad: str) -> None:
    """Both copies of the grammar must reject the same lines.

    The pre-flight only means anything if it checks the SAME grammar the gateway
    applies; a laxer gateway would let a bad line through when the agent is
    bypassed, and a stricter one would silently reject lines the pre-flight
    cleared.
    """
    line = f'{bad} <{VOCAB}mapsToColumn> "x" <{GRAPH}> .'
    assert _NQUAD_PATTERN.match(line) is None
    assert index.NQUAD_PATTERN.match(line) is None
