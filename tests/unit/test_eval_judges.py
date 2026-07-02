"""Unit tests for the shared custom-evaluator factory.

agents/shared/eval_judges.py is the single source of truth for the three custom
SESSION LLM-as-Judge evaluators (GoalSuccess, FinalAnswerFaithfulness,
SqlGrounded) that notebooks 2 (RAG), 6 (VKG), and 11 (VKG) register. These tests
pin the contract without calling AWS: the factory creates exactly three SESSION
judges in the right order, on the right model/scale, and selects RAG-vs-VKG
prompt families correctly (so the notebooks can never drift apart again).
"""
import pytest

from agents.shared.eval_judges import (
    BINARY_SCALE,
    JUDGE_MODEL_ID,
    RAG_JUDGE_PROMPTS,
    VKG_JUDGE_PROMPTS,
    create_custom_judges,
)


class _MockControlClient:
    """Records create_evaluator calls and returns a deterministic id per name."""

    def __init__(self):
        self.calls = []

    def create_evaluator(self, **kwargs):
        self.calls.append(kwargs)
        return {"evaluatorId": kwargs["evaluatorName"] + "-id"}


def test_returns_three_ids_in_canonical_order():
    """The factory returns [GoalSuccess, FAF, SqlGrounded] ids, in order.

    GoalSuccess is first (the headline metric, replacing the un-editable
    Builtin.GoalSuccessRate); ToolCallOrdering remains removed.
    """
    client = _MockControlClient()
    ids = create_custom_judges(control_client=client, family="rag", name_suffix="s1")
    assert ids == [
        "GoalSuccess_s1-id",
        "FinalAnswerFaithfulness_s1-id",
        "SqlGrounded_s1-id",
    ]
    assert len(client.calls) == 3


def test_all_judges_are_session_binary_on_judge_model():
    """Every created judge is SESSION-level, binary-scaled, on JUDGE_MODEL_ID."""
    client = _MockControlClient()
    create_custom_judges(control_client=client, family="vkg", name_suffix="s2")
    for call in client.calls:
        assert call["level"] == "SESSION"
        judge = call["evaluatorConfig"]["llmAsAJudge"]
        assert judge["ratingScale"] == BINARY_SCALE
        model = judge["modelConfig"]["bedrockEvaluatorModelConfig"]
        assert model["modelId"] == JUDGE_MODEL_ID
        # 4096 (raised from 1024): JUDGE_MODEL_ID is Sonnet 5, which has adaptive
        # thinking on by default — thinking tokens share the OUTPUT budget, so a
        # 1024 cap risks truncating the binary verdict before it emits.
        assert model["inferenceConfig"]["maxTokens"] == 4096


def test_rag_and_vkg_register_different_prompts():
    """The two families select distinct instruction text (not silently the same)."""
    rag = _MockControlClient()
    vkg = _MockControlClient()
    create_custom_judges(control_client=rag, family="rag", name_suffix="x")
    create_custom_judges(control_client=vkg, family="vkg", name_suffix="x")

    def _instr(client, prefix):
        for c in client.calls:
            if c["evaluatorName"].startswith(prefix):
                return c["evaluatorConfig"]["llmAsAJudge"]["instructions"]
        raise AssertionError(f"no judge named {prefix}")

    rag_faf = _instr(rag, "FinalAnswerFaithfulness")
    vkg_faf = _instr(vkg, "FinalAnswerFaithfulness")
    assert rag_faf != vkg_faf
    # The VKG FAF prompt carries the "which span is the final answer" guidance the
    # RAG one does not (the VKG path emits intermediate SPARQL/grounding spans).
    assert "virtual-knowledge-graph" in vkg_faf
    assert "which span is the agent's final answer" in vkg_faf
    assert "virtual-knowledge-graph" not in rag_faf

    # GoalSuccess likewise differs by family: the VKG variant must teach the judge
    # to ignore intermediate SPARQL spans, which the RAG variant does not mention.
    rag_goal = _instr(rag, "GoalSuccess")
    vkg_goal = _instr(vkg, "GoalSuccess")
    assert rag_goal != vkg_goal
    assert "virtual-knowledge-graph" in vkg_goal
    assert "SPARQL" in vkg_goal
    assert "SPARQL" not in rag_goal
    # Both families' GoalSuccess must key off the deterministic-graph final-answer
    # record (the whole point of replacing Builtin.GoalSuccessRate).
    assert "Final-answer record" in rag_goal
    assert "Final-answer record" in vkg_goal


def test_registered_instructions_match_prompt_constants():
    """What the factory sends == the exported prompt constants (no transform)."""
    client = _MockControlClient()
    create_custom_judges(control_client=client, family="rag", name_suffix="x")
    sent = {
        c["evaluatorName"].rsplit("_", 1)[0]: c["evaluatorConfig"]["llmAsAJudge"][
            "instructions"
        ]
        for c in client.calls
    }
    assert sent == RAG_JUDGE_PROMPTS


def test_all_session_placeholders_are_legal():
    """Every brace-delimited token is a legal SESSION placeholder.

    AWS ``CreateEvaluator`` parses ANY ``{...}`` in the instructions as a
    placeholder and rejects unknown ones — including stray braces from an
    embedded JSON example like ``{"intent": ...}`` (which is why those must be
    described in prose, not pasted literally). So we match ``\\{[^{}]*\\}``
    (any single-brace span), not just ``\\{\\w+\\}``, and require the inner token
    to be one of the allowed SESSION placeholders. ``{{`` / ``}}`` (escaped
    literal braces) are stripped first since they are not placeholders.
    """
    # SESSION evaluators may reference ONLY these placeholders.
    legal = {"context", "available_tools", "assertions",
             "expected_tool_trajectory", "actual_tool_trajectory"}
    import re

    for prompts in (RAG_JUDGE_PROMPTS, VKG_JUDGE_PROMPTS):
        for name, text in prompts.items():
            stripped = text.replace("{{", "").replace("}}", "")
            used = set(re.findall(r"\{([^{}]*)\}", stripped))
            illegal = used - legal
            assert not illegal, (
                f"{name} contains brace tokens AWS will reject as placeholders: "
                f"{illegal} (describe JSON examples in prose; no literal braces)"
            )


def test_unknown_family_raises():
    """A typo'd family fails loudly rather than silently picking a rubric."""
    client = _MockControlClient()
    with pytest.raises(ValueError, match="unknown judge family"):
        create_custom_judges(control_client=client, family="ontology")
    assert client.calls == []  # nothing created on the bad path


def test_blank_suffix_autogenerates_unique_names():
    """An empty name_suffix falls back to a fresh uuid, keeping redeploys unique."""
    a = _MockControlClient()
    b = _MockControlClient()
    create_custom_judges(control_client=a, family="rag")
    create_custom_judges(control_client=b, family="rag")
    a_names = {c["evaluatorName"] for c in a.calls}
    b_names = {c["evaluatorName"] for c in b.calls}
    assert a_names.isdisjoint(b_names)
