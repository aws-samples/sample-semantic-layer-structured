"""Drift detector for `agents/<name>/models.json` manifests.

Each agent that calls `bedrock:InvokeModel` declares the foundation models it
uses in `agents/<name>/models.json`. The CDK stack
(`cdk/lib/stacks/backend/agentcore-stack.ts`) reads these manifests to derive
IAM `bedrock:InvokeModel` grants — so a model used in code but missing from
the manifest will deploy with a missing IAM permission.

This test scans every `*.py` file under `agents/<name>/` for:

  1. Bare `model_id="<literal>"` kwargs to `BedrockModel(...)` / `Agent(...)`.
  2. Module-level `MODEL_ID = "<literal>"` / `EMBEDDING_MODEL_ID = "<literal>"`
     style constants that look like Bedrock model identifiers.

…and asserts every literal it finds is declared in the agent's `models.json`.

When this test fails:

  * Add the missing model id to `agents/<name>/models.json`, OR
  * If the literal is wrong (e.g. `"claude-sonnet-4-6"` instead of
    `"global.anthropic.claude-sonnet-4-6"`), fix the literal — the test will
    have caught a real Bedrock validation bug pre-deploy.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Set

import pytest


# Literal patterns that look like Bedrock identifiers — broad on purpose to
# catch typos like "claude-sonnet-4-6" (no provider prefix) which would still
# look "valid enough" to a human reviewer but break at ConverseStream time.
_BEDROCK_LITERAL_RE = re.compile(
    r'(?:'
    # Inference-profile ids carry a version suffix with a colon (e.g.
    # `global.anthropic.claude-haiku-4-5-20251001-v1:0`), so the regional
    # prefixes must allow ':' too — otherwise such ids escape detection and a
    # missing IAM grant ships silently.
    r'global\.[\w.\-:]+'
    r'|us\.[\w.\-:]+'
    r'|eu\.[\w.\-:]+'
    r'|apac\.[\w.\-:]+'
    r'|anthropic\.[\w.\-:]+'
    r'|amazon\.[\w.\-:]+'
    r'|meta\.[\w.\-:]+'
    r'|cohere\.[\w.\-:]+'
    r'|claude[\w.\-:]+'
    r')',
)

# Kwarg patterns we treat as a model declaration.
_MODEL_ID_KWARG_RE = re.compile(
    r'model_id\s*=\s*["\']([^"\']+)["\']',
)
_MODEL_ID_CONST_RE = re.compile(
    r'^\s*(?:[A-Z_]*MODEL_ID|EMBEDDING_MODEL_ID)\s*=\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)


def _agents_root() -> Path:
    """Resolve the repository's `agents/` directory from this test file."""
    # tests/unit/test_model_manifests.py → repo root → agents/
    return Path(__file__).resolve().parents[2] / 'agents'


def _agent_dirs() -> list[Path]:
    """Return every immediate subdirectory of `agents/` that has a `*.py` file
    AND is not the `shared/` helper directory.

    `shared/` does not run as its own runtime — its model usage is exercised
    by whichever agent imports it (currently `*_query_agent`) so its grants
    are covered there.
    """
    root = _agents_root()
    return sorted(
        d for d in root.iterdir()
        if d.is_dir()
        and d.name not in {'shared', '__pycache__'}
        and any(d.glob('**/*.py'))
    )


def _literals_in_file(py: Path) -> Set[str]:
    """Extract every plausibly-Bedrock-model-id string literal from a file."""
    src = py.read_text(encoding='utf-8', errors='replace')
    found: Set[str] = set()
    for m in _MODEL_ID_KWARG_RE.finditer(src):
        found.add(m.group(1))
    for m in _MODEL_ID_CONST_RE.finditer(src):
        found.add(m.group(1))
    return {
        s for s in found
        if _BEDROCK_LITERAL_RE.fullmatch(s)
    }


def _declared_models(agent_dir: Path) -> Set[str]:
    """Read `agents/<name>/models.json` and return the declared models."""
    manifest = agent_dir / 'models.json'
    if not manifest.exists():
        return set()
    data = json.loads(manifest.read_text(encoding='utf-8'))
    return set(data.get('foundation_models', []))


@pytest.mark.parametrize('agent_dir', _agent_dirs(), ids=lambda d: d.name)
def test_agent_model_literals_declared(agent_dir: Path) -> None:
    """Every Bedrock model literal in *.py is declared in models.json."""
    declared = _declared_models(agent_dir)
    used: Set[str] = set()
    for py in agent_dir.rglob('*.py'):
        if '__pycache__' in py.parts:
            continue
        used.update(_literals_in_file(py))

    # Also scan agents/shared/embedding.py — the embed model lives there but
    # is callable from any *_query_agent that imports it. Attribute it to
    # every agent that imports `agents.shared.embedding` or `shared.embedding`.
    shared_embed = _agents_root() / 'shared' / 'embedding.py'
    if shared_embed.exists():
        embed_literals = _literals_in_file(shared_embed)
        importers_pattern = re.compile(
            r'from\s+(?:agents\.)?shared\.embedding\s+import|'
            r'import\s+(?:agents\.)?shared\.embedding',
        )
        if any(
            importers_pattern.search(
                p.read_text(encoding='utf-8', errors='replace')
            )
            for p in agent_dir.rglob('*.py')
            if '__pycache__' not in p.parts
        ):
            used.update(embed_literals)

    if not used:
        pytest.skip(f'{agent_dir.name} declares no Bedrock model literals')

    missing = used - declared
    assert not missing, (
        f'{agent_dir.name}: model id(s) used in code but missing from '
        f'agents/{agent_dir.name}/models.json: {sorted(missing)}.\n'
        f'Either add them to the manifest or fix the literal in code.'
    )


@pytest.mark.parametrize('agent_dir', _agent_dirs(), ids=lambda d: d.name)
def test_no_unprefixed_model_ids(agent_dir: Path) -> None:
    """Catch the `model_id="claude-sonnet-4-6"` class of bug — a bare model
    name with no provider prefix or inference profile is never valid for
    `ConverseStream` and surfaces as ValidationException at runtime.
    """
    bad: list[tuple[Path, int, str]] = []
    for py in agent_dir.rglob('*.py'):
        if '__pycache__' in py.parts:
            continue
        for i, line in enumerate(
            py.read_text(encoding='utf-8', errors='replace').splitlines(),
            start=1,
        ):
            for m in _MODEL_ID_KWARG_RE.finditer(line):
                literal = m.group(1)
                if literal.startswith(('claude-', 'opus-', 'sonnet-', 'haiku-')):
                    bad.append((py, i, literal))
    assert not bad, (
        f'{agent_dir.name}: unprefixed model_id literal(s) found — '
        f'Bedrock requires a provider prefix or inference profile id:\n'
        + '\n'.join(f'  {p}:{i}: model_id="{lit}"' for p, i, lit in bad)
    )
