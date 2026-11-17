"""Isolation guard: the two Hybrid agents must stay fully self-contained.

Neither ``semantic_layer_query_agent`` nor ``semantic_layer_creation_agent`` may
import from ``agents/shared`` or any sibling agent package — the design's
copy-and-own rule. This test AST-scans every module in both packages and fails
on any banned import. It is written before the packages hold any logic and must
stay green through every subsequent task.
"""
import ast
import pathlib

import pytest

PKGS = ["semantic_layer_query_agent", "semantic_layer_creation_agent"]
BANNED = {
    "agents.shared", "shared",
    "ontology_query_agent", "metadata_query_agent",
    "ontology_agent", "metadata_agent", "query_suggestions_agent",
}


def _imported_modules(pkg: str):
    """Yield every module name imported by any ``*.py`` under ``agents/<pkg>``.

    Args:
        pkg: The agent package directory name under ``agents/``.

    Yields:
        str: Each absolute (level 0) imported module name.
    """
    root = pathlib.Path("agents") / pkg
    for f in root.rglob("*.py"):
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                yield from (a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                yield n.module


@pytest.mark.parametrize("pkg", PKGS)
def test_no_cross_imports(pkg: str) -> None:
    """Fail if a Hybrid agent imports shared or sibling-agent packages."""
    for m in _imported_modules(pkg):
        top = m.split(".")[0]
        assert m not in BANNED and top not in BANNED, f"{pkg} imports {m}"


@pytest.mark.parametrize("pkg", PKGS)
def test_no_self_referential_absolute_imports(pkg: str) -> None:
    """Fail if a Hybrid module imports its OWN package via the ``agents.`` prefix.

    The hybrid Dockerfiles copy ONLY ``<pkg>/`` into ``/app`` (there is no
    ``agents/`` parent in the image), so an ``from agents.<pkg>.x import y`` line
    raises ``ModuleNotFoundError: No module named 'agents'`` at runtime even
    though it resolves fine in the repo, where ``agents/`` is on the path. Use
    relative imports (``from .x import y``), which resolve under BOTH layouts.

    Args:
        pkg: The agent package directory name under ``agents/``.

    Returns:
        None.
    """
    root = pathlib.Path("agents") / pkg
    offenders: list[str] = []
    for f in root.rglob("*.py"):
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                if n.module.split(".")[0] == "agents":
                    offenders.append(f"{f}:{n.lineno} from {n.module}")
            elif isinstance(n, ast.Import):
                offenders += [
                    f"{f}:{n.lineno} import {a.name}"
                    for a in n.names
                    if a.name.split(".")[0] == "agents"
                ]
    assert not offenders, (
        "absolute 'agents.*' imports break inside the container image "
        "(use relative imports instead):\n  " + "\n  ".join(offenders)
    )
