"""The N-Quads literal grammar shared by the producer, the gateway and the viewer.

A Hybrid layer's thin VKG carries a one-line ``rdfs:comment`` per class and property,
harvested from the Knowledge Base markdown. Real descriptions quote things — the
``sets`` overview says a set is *(also called "expansions" or "editions")* — so
``vkg_builder.nquads_literal`` escapes the inner quotes to ``\\"``, which is valid
N-Quads.

The gateway's parser did not accept it. Its literal alternative was ``"[^"]*"``,
which rejects any line with an inner quote, and unmatched lines were skipped
*silently* while the tool still reported success. That combination turns a legal
payload into a partially-materialized graph indistinguishable from a clean build.
Four of 812 lines hit it on the 2026-08-15 rebuild of layer 51e29707.

These tests pin the three properties that make that class of failure impossible:

1. the grammar accepts every escape a producer can legally emit;
2. the gateway refuses the WHOLE payload when a line really is malformed, rather
   than inserting the subset that parsed;
3. the producer's pre-flight copy of the grammar and the gateway's are the same
   pattern, so the check cannot drift away from what it is checking.
"""

import importlib.util
import json
import os
import re
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from agents.semantic_layer_creation_agent.neptune_load import (
    _NQUAD_PATTERN,
    validate_nquads,
)
from agents.semantic_layer_creation_agent.vkg_builder import (
    build_thin_vkg_nquads,
    nquads_literal,
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
    "neptune_tools_index_escapes", _NEPTUNE_TOOLS_INDEX_PATH
)
index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index)

GRAPH = "<https://semantic-layer.aws/vkg/hybrid-acme>"
SUBJECT = "<https://semantic-layer.aws/virtual-kg/sets>"
COMMENT = "<http://www.w3.org/2000/01/rdf-schema#comment>"


def _quad(obj: str) -> str:
    """Build one N-Quads line with ``obj`` in the object position."""
    return f"{SUBJECT} {COMMENT} {obj} {GRAPH} ."


class _FakeResponse:
    """Minimal requests.Response stand-in for the SPARQL Update write path."""

    status_code = 200
    text = "{}"


# --------------------------------------------------------------------------- #
# The grammar itself.                                                         #
# --------------------------------------------------------------------------- #
# Every object form a producer in this repo can legally emit. Each must be
# accepted by BOTH the gateway and the pre-flight — a rejection here is a line
# that never reaches Neptune.
ACCEPTED_OBJECTS = [
    "<https://semantic-layer.aws/virtual-kg/cards>",
    '"a plain description"',
    '"quotes an \\"expansion\\" inline"',
    '"holds a \\\\ backslash"',
    '"wraps\\nonto a second line"',
    '"tab\\tseparated"',
    '"42"^^<http://www.w3.org/2001/XMLSchema#integer>',
    '"tagged"@en',
    '"tagged"@en-GB',
]

# Forms that are genuinely malformed. The gateway MUST refuse these rather than
# skip them, because a skipped line is a hole in the graph nobody is told about.
REJECTED_LINES = [
    '<https://x/A> <https://x/p> "no graph position" .',  # 3 terms, not 4
    f'{SUBJECT} {COMMENT} "unterminated {GRAPH} .',
    f"{SUBJECT} {COMMENT} bare-token {GRAPH} .",
    f"{SUBJECT} {COMMENT} \"trailing dot missing\" {GRAPH}",
]


@pytest.mark.parametrize("obj", ACCEPTED_OBJECTS)
def test_the_gateway_accepts_every_legal_object_form(obj: str):
    assert index.NQUAD_PATTERN.match(_quad(obj)), obj


@pytest.mark.parametrize("obj", ACCEPTED_OBJECTS)
def test_the_preflight_accepts_every_legal_object_form(obj: str):
    assert validate_nquads(_quad(obj)) == 1


@pytest.mark.parametrize("line", REJECTED_LINES)
def test_both_parsers_reject_a_malformed_line(line: str):
    assert index.NQUAD_PATTERN.match(line) is None, line
    with pytest.raises(ValueError, match="REJECTED"):
        validate_nquads(line)


def test_the_preflight_grammar_is_the_gateway_grammar():
    """The pre-flight is only meaningful while it mirrors what it validates against.

    Comparing the compiled patterns catches the drift that produced the original
    bug: one side was widened and the other was not.
    """
    assert _NQUAD_PATTERN.pattern == index.NQUAD_PATTERN.pattern


def test_a_quote_inside_a_literal_does_not_end_it():
    """The greedy trap: ``"a \\" b"`` must parse as ONE literal, not stop at the escape."""
    match = index.NQUAD_PATTERN.match(_quad('"a \\" b"'))
    assert match is not None
    assert match.group(3) == '"a \\" b"'


# --------------------------------------------------------------------------- #
# The producer's real output.                                                  #
# --------------------------------------------------------------------------- #
def test_a_harvested_description_with_quotes_survives_the_round_trip():
    """This exact description failed the 2026-08-15 rebuild of layer 51e29707."""
    description = (
        'A set is a released group of cards (also called "expansions" or '
        '"editions"). One row = one set.'
    )
    nquads = build_thin_vkg_nquads(
        table="sets",
        columns=[{"name": "code", "type": "string", "comment": 'the "short" code'}],
        class_iri="https://semantic-layer.aws/virtual-kg/sets",
        graph_iri="https://semantic-layer.aws/vkg/hybrid-acme",
        class_comment=description,
    )
    # Every line the builder emitted is loadable, and the description is intact.
    assert validate_nquads(nquads) == len(
        [line for line in nquads.strip().split("\n") if line.strip()]
    )
    assert nquads_literal(description) in nquads


def test_a_multiline_description_stays_one_physical_line():
    """A raw newline would split the quad in two and break the line-oriented parsers."""
    nquads = build_thin_vkg_nquads(
        table="sets",
        columns=[{"name": "code", "type": "string"}],
        class_iri="https://semantic-layer.aws/virtual-kg/sets",
        graph_iri="https://semantic-layer.aws/vkg/hybrid-acme",
        class_comment="First line.\nSecond line.",
    )
    assert "\\n" in nquads
    validate_nquads(nquads)  # raises if the quad was split


# --------------------------------------------------------------------------- #
# The gateway tool end to end.                                                 #
# --------------------------------------------------------------------------- #
def _persist(nquad_data: str) -> Dict[str, Any]:
    """Run tool_persist_to_neptune with the SigV4/HTTP seams stubbed.

    Args:
        nquad_data: The payload to persist.

    Returns:
        dict: The tool's parsed JSON result, plus ``_updates`` — the SPARQL
            strings that would have been POSTed to Neptune (empty if none were).
    """
    updates: List[str] = []

    def _post(url, headers=None, data=None, timeout=None):
        """Record the SPARQL Update instead of calling Neptune."""
        updates.append(data)
        return _FakeResponse()

    with patch.object(index, "get_neptune_endpoint", lambda: "https://neptune.local"):
        with patch.object(index.requests, "post", _post):
            with patch.object(index.SigV4Auth, "add_auth", lambda self, request: None):
                result = json.loads(index.tool_persist_to_neptune(nquad_data))
    result["_updates"] = updates
    return result


def test_persist_loads_a_description_containing_quotes():
    payload = _quad('"quotes an \\"expansion\\" inline"')
    result = _persist(payload)
    assert result["success"] is True
    assert len(result["_updates"]) == 1
    # The escaped quote must reach Neptune verbatim; re-escaping or stripping it
    # would either corrupt the SPARQL or silently alter the description.
    assert '\\"expansion\\"' in result["_updates"][0]


def test_persist_refuses_the_whole_payload_when_one_line_is_malformed():
    """Loading the parseable subset yields a partial graph reported as success."""
    payload = "\n".join([_quad('"good"'), "<https://x/A> <https://x/p> \"no graph\" ."])
    result = _persist(payload)
    assert result["success"] is False
    assert "1 of 2" in result["message"]
    assert "no graph" in result["message"]
    # Nothing at all was written.
    assert result["_updates"] == []


def test_persist_groups_by_named_graph():
    other = "<https://semantic-layer.aws/vkg/other>"
    payload = "\n".join(
        [_quad('"in acme"'), f'{SUBJECT} {COMMENT} "in other" {other} .']
    )
    result = _persist(payload)
    assert result["success"] is True
    update = result["_updates"][0]
    assert update.count("GRAPH") == 2
    assert GRAPH in update and other in update


def test_persist_reports_an_empty_payload_rather_than_writing_nothing_quietly():
    result = _persist("   \n\n")
    assert result["success"] is False
    assert "No valid n-quads" in result["message"]
    assert result["_updates"] == []


# --------------------------------------------------------------------------- #
# The viewer reads the same lines back.                                        #
# --------------------------------------------------------------------------- #
def test_the_frontend_parser_shares_the_literal_grammar():
    """``OntologyEditor.jsx`` re-parses the artifact for the Metadata tab.

    A description the gateway accepts but the viewer drops shows the same em-dash
    as no description at all, so the two grammars must agree on escapes.
    """
    editor = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "frontend",
            "src",
            "components",
            "OntologyEditor.jsx",
        )
    )
    with open(editor, "r", encoding="utf-8") as handle:
        source = handle.read()
    # The escape-aware literal alternative, as written in the JSX regex literal.
    assert re.search(r'"\(\?:\[\^"\\\\\]\|\\\\\.\)\*"', source), (
        "OntologyEditor.jsx's parseNQuads no longer accepts escaped literals; "
        "descriptions containing a quote would render as an em-dash."
    )
