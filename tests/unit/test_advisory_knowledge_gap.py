"""Advisory knowledge_gap — naming the missing input instead of guessing it.

This is the "advise rather than evaluate" half of the runtime decision: when a
governed rule matches the question but cannot be evaluated (an unmapped input, or
a threshold no human has approved), the answer must NAME what is missing rather
than substitute a plausible bound. The parameter is additive — every pre-existing
advisory caller and test must keep working untouched.
"""
from typing import Any, Dict, List

from agents.shared.advisory import build_advisory_answer


class _FakeTable:
    """Metrics table stand-in returning no governed metrics."""

    def query(self, **_kw: Any) -> Dict[str, List[Any]]:
        """Return an empty DDB query result."""
        return {"Items": []}


def _call(**over: Any) -> Dict[str, Any]:
    """Invoke build_advisory_answer with baseline stubs, overriding via kwargs."""
    kwargs: Dict[str, Any] = {
        "question": "which holdings are at lapse risk?",
        "layer_id": "ont-1",
        "kb_retrieve": lambda q: {"retrievalResults": []},
        "metrics_table": _FakeTable(),
        "synthesize": lambda prompt: "advisory text",
        "layer_name": "ACME",
    }
    kwargs.update(over)
    return build_advisory_answer(**kwargs)


def test_no_knowledge_gap_keeps_the_existing_shape() -> None:
    """The default path is unchanged: no gap key content, no SQL, no rows."""
    out = _call()
    assert out["executed_sql"] == ""
    assert out["results"] == []
    assert out.get("knowledge_gap") is None


def test_a_knowledge_gap_is_echoed_on_the_response() -> None:
    """The caller gets the gap back structurally, not parsed out of prose."""
    gap = {
        "rule_id": "RULE-004",
        "rule_name": "lapse risk after non-payment",
        "missing_inputs": ["https://ex.org/vkg#daysSinceLastPayment"],
        "unapproved_thresholds": ["THR-1"],
    }
    out = _call(knowledge_gap=gap)
    assert out["knowledge_gap"] == gap


def test_the_gap_reaches_the_prompt() -> None:
    """The model is told which rule matched and which input is unmapped."""
    seen: List[str] = []
    _call(
        synthesize=lambda prompt: seen.append(prompt) or "text",
        knowledge_gap={
            "rule_id": "RULE-004", "rule_name": "lapse risk",
            "missing_inputs": ["https://ex.org/vkg#daysSinceLastPayment"],
            "unapproved_thresholds": [],
        },
    )
    assert "daysSinceLastPayment" in seen[0]
    assert "RULE-004" in seen[0]


def test_the_prompt_forbids_guessing_the_missing_value() -> None:
    """Principle 5 stated to the model, not only enforced upstream."""
    seen: List[str] = []
    _call(
        synthesize=lambda prompt: seen.append(prompt) or "text",
        knowledge_gap={"rule_id": "R", "rule_name": "n",
                       "missing_inputs": ["https://ex.org/vkg#x"],
                       "unapproved_thresholds": []},
    )
    lowered = seen[0].lower()
    assert "do not guess" in lowered or "never guess" in lowered


def test_a_gap_answer_still_executes_no_sql() -> None:
    """The structural no-SQL guarantee is unaffected by the gap path."""
    out = _call(knowledge_gap={"rule_id": "R", "rule_name": "n",
                               "missing_inputs": ["x"], "unapproved_thresholds": []})
    assert out["executed_sql"] == ""
    assert out["results"] == []


def test_an_unapproved_threshold_alone_is_reported() -> None:
    """An unapproved bound is a gap even when every input IS mapped."""
    seen: List[str] = []
    _call(
        synthesize=lambda prompt: seen.append(prompt) or "text",
        knowledge_gap={"rule_id": "R", "rule_name": "lapse risk",
                       "missing_inputs": [], "unapproved_thresholds": ["THR-1"]},
    )
    assert "THR-1" in seen[0]


def test_an_empty_gap_adds_no_prompt_block() -> None:
    """A gap dict with nothing missing must not emit a "not evaluable" block.

    The caller builds this dict from the rule, so an all-empty one is reachable;
    rendering the header anyway would tell the user a rule failed when it did not.
    """
    seen: List[str] = []
    _call(
        synthesize=lambda prompt: seen.append(prompt) or "text",
        knowledge_gap={"rule_id": "R", "rule_name": "n",
                       "missing_inputs": [], "unapproved_thresholds": []},
    )
    assert "NOT EVALUABLE" not in seen[0]


def test_an_empty_model_answer_falls_back_to_naming_the_gap() -> None:
    """The deterministic fallback uses the gap instead of the generic text.

    With an empty KB and no metrics the existing fallback says "no schema
    knowledge base content and no governed metrics" — which is true but useless
    here, because a rule DID match and we know exactly what it is waiting on.
    """
    out = _call(
        synthesize=lambda prompt: "   ",
        knowledge_gap={
            "rule_id": "RULE-004", "rule_name": "lapse risk after non-payment",
            "missing_inputs": ["https://ex.org/vkg#daysSinceLastPayment"],
            "unapproved_thresholds": [],
        },
    )
    assert "lapse risk after non-payment" in out["answer"]
    assert "daysSinceLastPayment" in out["answer"]
    # The generic dead-end text must not be what the user sees in this case.
    assert "no governed metrics defined" not in out["answer"]
