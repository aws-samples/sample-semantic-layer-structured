"""Export the reference-free ONLINE judge prompts to a JSON mirror.

``agents/shared/eval_judges.py`` is the single Python source of truth for every
custom LLM-as-Judge prompt. The CDK eval stack (``agentcore-eval-stack.ts``)
deploys the ONLINE (reference-free) subset onto the two query-agent online-eval
configs, but TypeScript cannot import a Python module — so this script exports
that subset to ``agents/shared/online_judge_prompts.json``, which the stack reads
at synth time. The deployed online judges are therefore byte-identical to the
canonical Python definitions.

Run this whenever an online judge prompt changes:

    python -m agents.shared.online_judge_prompts_export

The parity test ``tests/unit/test_online_judge_prompts.py`` re-runs ``build()``
and fails if the checked-in JSON is stale, so a forgotten regen cannot ship.
"""
from __future__ import annotations

import json
import os
from typing import Dict

from agents.shared.eval_judges import (
    ONLINE_RAG_JUDGE_PROMPTS,
    ONLINE_VKG_JUDGE_PROMPTS,
)

# The JSON mirror lives beside eval_judges.py; the CDK stack reads it via a
# repo-root-relative path (agents/shared/online_judge_prompts.json).
JSON_PATH = os.path.join(os.path.dirname(__file__), "online_judge_prompts.json")


def build() -> Dict[str, Dict[str, str]]:
    """Return the {family: {judge: prompt}} mapping the JSON mirror serializes.

    Keyed ``{"rag": {...}, "vkg": {...}}`` where each family carries the three
    reference-free online judges ``GoalSuccess`` / ``SqlGrounded`` /
    ``ToolCallOrdering``. The CDK ``OnlineJudgePrompts`` interface mirrors this
    exact shape.

    Returns:
        The nested prompt dict, ready for ``json.dumps``.
    """
    return {
        "rag": dict(ONLINE_RAG_JUDGE_PROMPTS),
        "vkg": dict(ONLINE_VKG_JUDGE_PROMPTS),
    }


def serialize(prompts: Dict[str, Dict[str, str]]) -> str:
    """Serialize the prompt mapping to the canonical on-disk JSON string.

    ``ensure_ascii=True`` + 2-space indent + trailing newline match what the
    parity test compares against, so re-running the exporter is byte-stable.

    Args:
        prompts: The ``build()`` mapping to serialize.

    Returns:
        The exact file content (including trailing newline).
    """
    return json.dumps(prompts, indent=2, ensure_ascii=True) + "\n"


def write() -> None:
    """Write the JSON mirror to ``online_judge_prompts.json``."""
    content = serialize(build())
    with open(JSON_PATH, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"wrote {JSON_PATH}")


if __name__ == "__main__":
    write()
