"""
Regression test for tool_get_ontology_from_neptune.

The query agent's column picker (build_schema_digest) consumes
{label, comment} per class and per property. If the parser drops these,
Pick-2 collapses to surface-level table-name matching.

This test fakes Neptune's SPARQL response and asserts the parser keeps
rdfs:label / rdfs:comment for both classes and datatype properties.
"""

import importlib.util
import json
import os
from unittest.mock import patch

# Load lambda/neptune-tools/index.py by file path under a UNIQUE module name.
# Several Lambda handlers under test are all named `index.py`; a plain
# `import index` caches the first one in sys.modules['index'] and hands later
# test files the WRONG module under the full-suite run. A distinct name avoids
# that cross-test collision.
_NEPTUNE_TOOLS_INDEX_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "neptune-tools", "index.py")
)
_spec = importlib.util.spec_from_file_location("neptune_tools_index", _NEPTUNE_TOOLS_INDEX_PATH)
index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index)


def _binding(s, p, o):
    return {
        "subject": {"value": s},
        "predicate": {"value": p},
        "object": {"value": o},
    }


def _fake_neptune_response():
    """rdf:type ordered before rdfs:label/comment, as the SPARQL ORDER BY produces."""
    G = "http://insurance/ontology/abc"
    P = f"{G}/Party"
    F = f"{G}/Party/firstName"
    return {
        "results": {
            "bindings": [
                # class
                _binding(P, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                         "http://www.w3.org/2002/07/owl#Class"),
                _binding(P, "http://www.w3.org/2000/01/rdf-schema#label", "Party"),
                _binding(P, "http://www.w3.org/2000/01/rdf-schema#comment",
                         "A person who holds at least one policy"),
                _binding(P, "https://semantic-layer.aws/virtual-kg/mapsToTable",
                         "insurance_db.party"),
                # datatype property
                _binding(F, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                         "http://www.w3.org/2002/07/owl#DatatypeProperty"),
                _binding(F, "http://www.w3.org/2000/01/rdf-schema#label", "first_name"),
                _binding(F, "http://www.w3.org/2000/01/rdf-schema#comment",
                         "Customer's given name"),
                _binding(F, "https://semantic-layer.aws/virtual-kg/mapsToColumn",
                         "party.first_name"),
            ]
        }
    }


def test_get_ontology_returns_labels_and_comments_for_classes_and_properties():
    with patch.object(index, "execute_sparql_query", return_value=_fake_neptune_response()), \
         patch.object(index, "_resolve_graph_uri", return_value="http://insurance/ontology/abc"):
        raw = index.tool_get_ontology_from_neptune("abc")

    payload = json.loads(raw)
    assert "error" not in payload, payload

    party_uri = "http://insurance/ontology/abc/Party"
    fname_uri = "http://insurance/ontology/abc/Party/firstName"

    assert payload["classes"][party_uri]["label"] == "Party"
    assert payload["classes"][party_uri]["comment"].startswith("A person")
    assert payload["properties"][fname_uri]["type"].endswith("DatatypeProperty")
    assert payload["properties"][fname_uri]["label"] == "first_name"
    assert payload["properties"][fname_uri]["comment"] == "Customer's given name"
    assert payload["mappings"][party_uri]["table"] == "insurance_db.party"
    assert payload["mappings"][fname_uri]["column"] == "party.first_name"


def test_get_ontology_handles_label_arriving_before_type():
    """Defensive: if the SPARQL ORDER BY ever changes, label-before-type must still work."""
    G = "http://insurance/ontology/abc"
    P = f"{G}/Party"
    label_first = {
        "results": {
            "bindings": [
                _binding(P, "http://www.w3.org/2000/01/rdf-schema#label", "Party"),
                _binding(P, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                         "http://www.w3.org/2002/07/owl#Class"),
            ]
        }
    }

    with patch.object(index, "execute_sparql_query", return_value=label_first), \
         patch.object(index, "_resolve_graph_uri", return_value=G):
        raw = index.tool_get_ontology_from_neptune("abc")

    payload = json.loads(raw)
    # When label arrives before type the subject was bucketed into 'properties';
    # once type=Class arrives we don't migrate it, but the label must not be lost.
    found = (payload["classes"].get(P, {}).get("label")
             or payload["properties"].get(P, {}).get("label"))
    assert found == "Party"
