"""Parity tests for the ONLINE judge-prompt JSON mirror.

The CDK eval stack (``cdk/lib/stacks/backend/agentcore-eval-stack.ts``) deploys
the reference-free online judges by reading
``agents/shared/online_judge_prompts.json`` at synth time. That JSON is a
GENERATED mirror of the canonical Python constants in
``agents/shared/eval_judges.py`` (TypeScript cannot import the Python module).

These tests guard the mirror so the two copies can never silently drift — the
exact failure that left the deployed online ``SqlGrounded`` judge without the
degraded-run pass branch while the notebook copy had it. If a prompt changes in
``eval_judges.py`` without re-running the exporter, ``test_checked_in_json_is_fresh``
fails with a clear "re-run the exporter" message.
"""
import json
import os

from agents.shared.eval_judges import (
    ONLINE_RAG_JUDGE_PROMPTS,
    ONLINE_VKG_JUDGE_PROMPTS,
    RAG_SQL_GROUNDED,
    RAG_TOOL_CALL_ORDERING,
    VKG_SQL_GROUNDED,
    VKG_TOOL_CALL_ORDERING,
)
from agents.shared.online_judge_prompts_export import JSON_PATH, build, serialize

# SESSION evaluators on LIVE traffic may reference ONLY these reference-FREE
# placeholders. {assertions} / {expected_response} / {expected_tool_trajectory} /
# {actual_tool_trajectory} are reference inputs — AgentCore rejects an ONLINE
# config that uses any of them. This is the whole reason the online GoalSuccess
# is a distinct prompt from the on-demand (assertion-reading) GoalSuccess.
_ONLINE_LEGAL_PLACEHOLDERS = {"context", "available_tools"}
_REFERENCE_PLACEHOLDERS = {
    "assertions",
    "expected_response",
    "expected_tool_trajectory",
    "actual_tool_trajectory",
}


def test_checked_in_json_is_fresh():
    """The committed JSON mirror equals a fresh export of the Python constants.

    If this fails, run:  python -m agents.shared.online_judge_prompts_export
    (the CDK stack reads the stale file, so a drift here ships wrong judges).
    """
    assert os.path.exists(JSON_PATH), f"missing generated mirror: {JSON_PATH}"
    with open(JSON_PATH, encoding="utf-8") as fh:
        on_disk = fh.read()
    expected = serialize(build())
    assert on_disk == expected, (
        "online_judge_prompts.json is stale — re-run "
        "`python -m agents.shared.online_judge_prompts_export` after editing "
        "the online judge prompts in eval_judges.py"
    )


def test_mirror_has_both_families_and_three_judges_each():
    """The mirror carries rag+vkg, each with the three online judges."""
    data = build()
    assert set(data) == {"rag", "vkg"}
    for family in ("rag", "vkg"):
        assert set(data[family]) == {"GoalSuccess", "SqlGrounded", "ToolCallOrdering"}


def test_online_grounding_and_ordering_reuse_ondemand_prompts():
    """SqlGrounded/ToolCallOrdering are ALREADY reference-free, so the online copy
    IS the on-demand copy (no separate reformulation — they must stay identical)."""
    assert ONLINE_RAG_JUDGE_PROMPTS["SqlGrounded"] == RAG_SQL_GROUNDED
    assert ONLINE_RAG_JUDGE_PROMPTS["ToolCallOrdering"] == RAG_TOOL_CALL_ORDERING
    assert ONLINE_VKG_JUDGE_PROMPTS["SqlGrounded"] == VKG_SQL_GROUNDED
    assert ONLINE_VKG_JUDGE_PROMPTS["ToolCallOrdering"] == VKG_TOOL_CALL_ORDERING


def test_sql_grounded_online_carries_degraded_run_branch():
    """Regression: the deployed online SqlGrounded MUST have the degraded-run pass
    branch (its absence in the old hand-copied TS was the core drift bug)."""
    for prompts in (ONLINE_RAG_JUDGE_PROMPTS, ONLINE_VKG_JUDGE_PROMPTS):
        assert "degraded" in prompts["SqlGrounded"].lower()
        assert "no sql executed" in prompts["SqlGrounded"].lower()


def test_all_online_prompts_are_reference_free():
    """No online judge may reference a ground-truth placeholder (AgentCore rejects
    such an evaluator from an online config). Match any single-brace span and
    require its inner token to be a reference-free SESSION placeholder."""
    import re

    for family, prompts in (("rag", ONLINE_RAG_JUDGE_PROMPTS),
                            ("vkg", ONLINE_VKG_JUDGE_PROMPTS)):
        for name, text in prompts.items():
            stripped = text.replace("{{", "").replace("}}", "")
            used = set(re.findall(r"\{([^{}]*)\}", stripped))
            forbidden = used & _REFERENCE_PLACEHOLDERS
            assert not forbidden, (
                f"{family}.{name} references reference-input placeholder(s) "
                f"{forbidden}; an online evaluator may use only "
                f"{_ONLINE_LEGAL_PLACEHOLDERS}"
            )
            illegal = used - _ONLINE_LEGAL_PLACEHOLDERS
            assert not illegal, (
                f"{family}.{name} has brace tokens AWS will reject as "
                f"placeholders: {illegal} (describe JSON examples in prose)"
            )


def test_no_unbalanced_braces():
    """Braces must be balanced (every '{' has a matching '}').

    The reference-free regex above matches balanced {token} spans, but a stray
    UNBALANCED brace (e.g. a half-pasted JSON example) wouldn't show up there
    while AgentCore's placeholder parser could still choke on it. Assert balance
    explicitly, ignoring escaped literal braces ({{ / }}).
    """
    for family, prompts in (("rag", ONLINE_RAG_JUDGE_PROMPTS),
                            ("vkg", ONLINE_VKG_JUDGE_PROMPTS)):
        for name, text in prompts.items():
            opens = text.count("{") - 2 * text.count("{{")
            closes = text.count("}") - 2 * text.count("}}")
            assert opens == closes, (
                f"{family}.{name} has unbalanced braces "
                f"(non-escaped {{={opens}, }}={closes}); a stray brace can make "
                f"AgentCore reject the evaluator at deploy"
            )


def test_online_goal_success_replaces_builtin_and_is_graph_aware():
    """The online GoalSuccess (the Builtin.GoalSuccessRate replacement) must teach
    the judge the deterministic-graph 'Final-answer record' marker, and the VKG
    variant must additionally warn off intermediate SPARQL spans."""
    rag_goal = ONLINE_RAG_JUDGE_PROMPTS["GoalSuccess"]
    vkg_goal = ONLINE_VKG_JUDGE_PROMPTS["GoalSuccess"]
    assert rag_goal != vkg_goal
    for goal in (rag_goal, vkg_goal):
        assert "Final-answer record" in goal
        # Reference-free framing: judges responsiveness/grounding, not a known answer.
        assert "no ground-truth answer is available" in goal
    assert "SPARQL" in vkg_goal
    assert "SPARQL" not in rag_goal
