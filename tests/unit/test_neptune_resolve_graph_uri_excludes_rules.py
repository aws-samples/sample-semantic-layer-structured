"""_resolve_graph_uri must never return the /rules sibling graph."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

_PATH = Path(__file__).resolve().parents[2] / "lambda" / "neptune-tools" / "index.py"


def _load_module():
    """Load the Lambda module by path — it is not an importable package."""
    spec = importlib.util.spec_from_file_location("neptune_tools_index", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bindings(*uris):
    return {"results": {"bindings": [{"g": {"value": u}} for u in uris]}}


ONT = "https://semantic-layer.aws/ontologies/abc-123"
RULES = "https://semantic-layer.aws/ontologies/abc-123/rules"


def test_returns_ontology_graph_when_only_it_exists() -> None:
    mod = _load_module()
    with patch.object(mod, "execute_sparql_query", return_value=_bindings(ONT)):
        assert mod._resolve_graph_uri("abc-123") == ONT


def test_rules_graph_is_not_returned_when_listed_first() -> None:
    """Regression: the rules graph sorting first must not win."""
    mod = _load_module()
    with patch.object(mod, "execute_sparql_query",
                      return_value=_bindings(RULES, ONT)):
        assert mod._resolve_graph_uri("abc-123") == ONT


def test_raises_when_only_the_rules_graph_exists() -> None:
    """Fail loudly rather than hand back a rules graph as the ontology."""
    mod = _load_module()
    with patch.object(mod, "execute_sparql_query", return_value=_bindings(RULES)):
        with pytest.raises(ValueError, match="No named graph"):
            mod._resolve_graph_uri("abc-123")
