import json

import pytest

from agents.shared.eval_multiturn import parse_chat_stream_sse
from agents.shared.eval_multiturn import (
    parse_multiturn_row, build_chat_payload, build_trajectory_assertions, run_key,
)
from agents.shared.eval_multiturn import build_scenarios, format_agent_output


def test_parse_sse_collects_answer_and_totals():
    body = (
        'data: {"type":"run_started","turnId":"t1"}\n\n'
        'data: {"type":"message_chunk","turnId":"t1","delta":"There are "}\n\n'
        'data: {"type":"message_chunk","turnId":"t1","delta":"15 parties."}\n\n'
        'data: {"type":"run_finished","turnId":"t1","messageId":"m-t1",'
        '"totals":{"sql":"SELECT COUNT(*) FROM party","rowCount":1,'
        '"rows":[{"c":15}],"usage":{"totalTokens":42},"runtimeMs":1200}}\n\n'
    )
    out = parse_chat_stream_sse(body)
    assert out["answer"] == "There are 15 parties."
    assert out["sql"] == "SELECT COUNT(*) FROM party"
    assert out["rows"] == [{"c": 15}]
    assert out["usage"] == {"totalTokens": 42}
    assert out["runtime_ms"] == 1200
    assert out["error"] is None
    assert out["clarification"] is None


def test_parse_sse_captures_clarification_and_error():
    body = (
        'data: {"type":"message_chunk","turnId":"t","delta":"Which one?"}\n\n'
        'data: {"type":"run_finished","turnId":"t","messageId":"m",'
        '"totals":{"clarification":{"options":[{"id":"party","label":"party"}]}}}\n\n'
    )
    out = parse_chat_stream_sse(body)
    assert out["clarification"] == {"options": [{"id": "party", "label": "party"}]}
    err = parse_chat_stream_sse('data: {"type":"run_error","turnId":"t","error":"boom"}\n\n')
    assert err["error"] == "boom"


def test_parse_sse_tolerates_malformed_and_keepalive_frames():
    # A non-JSON data line, a bare keep-alive comment, an empty data line, and an
    # event: line must all be skipped without crashing — the valid chunk + totals
    # still parse.
    body = (
        ': keep-alive\n\n'
        'data: not-json-at-all\n\n'
        'data:\n\n'
        'event: message_chunk\n'
        'data: {"type":"message_chunk","turnId":"t","delta":"hi"}\n\n'
        'data: {"type":"run_finished","turnId":"t","totals":{"sql":"SELECT 1"}}\n\n'
    )
    out = parse_chat_stream_sse(body)
    assert out["answer"] == "hi"
    assert out["sql"] == "SELECT 1"
    assert out["error"] is None


def test_single_turn_row_is_backward_compatible():
    row = {"Natural_Language_Question": "How many parties are there?",
           "Expected_Answer": "15", "Expected_SQL_Query": "SELECT 1",
           "Expected_SQL_Result": []}
    spec = parse_multiturn_row(row, index=3)
    assert spec["mode"] == "scripted"
    assert spec["scenario_id"] == "gt-row-03"
    assert [t["input"] for t in spec["turns"]] == ["How many parties are there?"]
    assert spec["trajectory_assertions"] == []  # no multiturn block


def test_scripted_multiturn_row():
    row = {"Natural_Language_Question": "How many are there?",
           "Expected_Answer": "15", "Expected_SQL_Query": "SELECT 1",
           "Expected_SQL_Result": [],
           "multiturn": {"mode": "scripted", "scenario_id": "mt-parties",
                         "turns": [{"input": "How many are there?"}, {"input": "party"}],
                         "trajectory_assertions": ["asked then answered"]}}
    spec = parse_multiturn_row(row, index=0)
    assert spec["scenario_id"] == "mt-parties"
    assert [t["input"] for t in spec["turns"]] == ["How many are there?", "party"]
    assert spec["trajectory_assertions"] == ["asked then answered"]


def test_build_chat_payload_shape():
    p = build_chat_payload(message="party", session_id="mt-parties-abc",
                           ontology_id="layer-1", turn_idx=2)
    assert p["message"] == "party" and p["sessionId"] == "mt-parties-abc"
    assert p["ontologyId"] == "layer-1" and p["mode"] == "semantic-rag"
    assert p["turnId"]  # non-empty, unique-ish per turn
    assert "question" not in p  # chat path reads 'message', not 'question'


def test_run_key_is_per_turn():
    assert run_key("s1", 0) != run_key("s1", 1)


def test_build_trajectory_assertions_appends_final_answer():
    spec = {"trajectory_assertions": ["a1"], "expected_answer": "There are 15 parties."}
    out = build_trajectory_assertions(spec)
    assert "a1" in out
    assert any("15 parties" in s for s in out)


def test_multiturn_block_with_empty_turns_raises():
    row = {"Natural_Language_Question": "q", "Expected_Answer": "a",
           "Expected_SQL_Query": "", "Expected_SQL_Result": [],
           "multiturn": {"mode": "scripted", "scenario_id": "mt-bad", "turns": []}}
    with pytest.raises(ValueError, match="mt-bad"):
        parse_multiturn_row(row, index=0)


def test_build_trajectory_assertions_without_expected_answer():
    # No expected_answer -> only the explicit assertions, no final-answer line.
    out = build_trajectory_assertions({"trajectory_assertions": ["only"], "expected_answer": ""})
    assert out == ["only"]


def test_dataset_loads_and_multiturn_rows_parse():
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[2] / "data/eval/groundtruth_dataset.json"
    rows = json.loads(p.read_text())
    assert isinstance(rows, list) and len(rows) >= 13  # 10 legacy + 3 new
    ids = [parse_multiturn_row(r, index=i)["scenario_id"] for i, r in enumerate(rows)]
    assert "mt-parties-clarify" in ids
    assert "mt-no-spurious-clarify" in ids
    assert "mt-stable-options" in ids
    # every legacy row still yields >=1 turn
    for i, r in enumerate(rows):
        assert len(parse_multiturn_row(r, index=i)["turns"]) >= 1


def test_build_scenarios_routes_modes():
    specs = [
        {"mode": "scripted", "scenario_id": "s1", "turns": [{"input": "a"}, {"input": "b"}],
         "trajectory_assertions": ["x"], "actor_profile": None, "max_turns": 4, "expected_answer": "A"},
        {"mode": "simulated", "scenario_id": "s2", "turns": [{"input": "go"}],
         "trajectory_assertions": [], "actor_profile": {"traits": {}, "context": "c", "goal": "g"},
         "max_turns": 3, "expected_answer": "B"},
    ]
    scn = build_scenarios(specs, ontology_id="L", simulated_enabled=True)
    assert scn[0].__class__.__name__ == "PredefinedScenario"
    assert [t.input for t in scn[0].turns] == ["a", "b"]
    assert scn[1].__class__.__name__ == "SimulatedScenario"
    # simulated disabled -> simulated scenario dropped, scripted kept
    scn2 = build_scenarios(specs, ontology_id="L", simulated_enabled=False)
    assert len(scn2) == 1 and scn2[0].scenario_id == "s1"


def test_format_agent_output_plain_answer():
    parsed = {"answer": "There are 15 parties.", "clarification": None, "error": None}
    assert format_agent_output(parsed) == "There are 15 parties."


def test_format_agent_output_folds_clarification_labels():
    parsed = {"answer": "Which interpretation do you mean?",
              "clarification": {"options": [{"id": "party", "label": "party (database: normalized)"},
                                            {"id": "relation", "label": "relation (database: normalized)"}]},
              "error": None}
    out = format_agent_output(parsed)
    assert "Which interpretation" in out
    assert "party (database: normalized)" in out
    assert "relation (database: normalized)" in out
    assert "CLARIFICATION" in out  # a marker the judge can key on


def test_format_agent_output_error_fallback():
    assert "boom" in format_agent_output({"answer": "", "clarification": None, "error": "boom"})


def test_build_scenarios_simulated_fields_wired():
    specs = [{"mode": "simulated", "scenario_id": "s2", "turns": [{"input": "go"}],
              "trajectory_assertions": ["t"], "actor_profile": {"traits": {"x": 1}, "context": "c", "goal": "g"},
              "max_turns": 5, "expected_answer": "B"}]
    scn = build_scenarios(specs, ontology_id="L", simulated_enabled=True)
    s = scn[0]
    assert s.input == "go"
    assert s.max_turns == 5
    assert s.actor_profile.context == "c" and s.actor_profile.goal == "g"
    assert s.metadata == {"ontologyId": "L"}
    # final-answer assertion appended
    assert any("B" in a for a in s.assertions)


def test_build_scenarios_scripted_metadata_and_trajectory():
    specs = [{"mode": "scripted", "scenario_id": "s1", "turns": [{"input": "a"}],
              "trajectory_assertions": [], "actor_profile": None, "max_turns": 4, "expected_answer": ""}]
    s = build_scenarios(specs, ontology_id="LX", simulated_enabled=True)[0]
    assert s.metadata == {"ontologyId": "LX"}
    assert s.expected_trajectory == ["execute_sql_query"]


def test_build_scenarios_expected_response_on_final_turn_only():
    # expected_response is TRACE-level GT; in a multi-turn conversation only the
    # last turn produces the answer, so earlier (clarification) turns must NOT carry it.
    specs = [{"mode": "scripted", "scenario_id": "mt", "turns": [{"input": "a"}, {"input": "b"}],
              "trajectory_assertions": ["x"], "actor_profile": None, "max_turns": 4,
              "expected_answer": "There are 15 parties."}]
    s = build_scenarios(specs, ontology_id="L", simulated_enabled=True)[0]
    assert s.turns[0].expected_response is None
    assert s.turns[1].expected_response == "There are 15 parties."


def test_build_scenarios_no_expected_response_when_answer_blank():
    specs = [{"mode": "scripted", "scenario_id": "s1", "turns": [{"input": "a"}],
              "trajectory_assertions": [], "actor_profile": None, "max_turns": 4, "expected_answer": ""}]
    s = build_scenarios(specs, ontology_id="L", simulated_enabled=True)[0]
    assert s.turns[0].expected_response is None


def test_trajectory_assertions_fallback_from_expect_clarification():
    # No curated prose -> per-turn expect_clarification flags become assertions so
    # the structured ground truth still reaches the SESSION judge.
    spec = {
        "trajectory_assertions": [],
        "turns": [{"input": "How many are there?", "expect_clarification": True},
                  {"input": "party", "expect_clarification": False}],
        "expected_answer": "There are 15 parties.",
    }
    out = build_trajectory_assertions(spec)
    assert any("turn 1" in s and "clarifying" in s for s in out)
    assert any("turn 2" in s and "without" in s for s in out)
    assert any("15 parties" in s for s in out)


def test_trajectory_assertions_curated_prose_skips_fallback():
    # When curated prose exists, we do NOT synthesize duplicate flag-derived lines.
    spec = {
        "trajectory_assertions": ["curated assertion"],
        "turns": [{"input": "q", "expect_clarification": True}],
        "expected_answer": "",
    }
    out = build_trajectory_assertions(spec)
    assert out == ["curated assertion"]


def test_format_agent_output_empty_options_no_marker():
    # clarification present but no option labels -> no spurious [CLARIFICATION] marker
    out = format_agent_output({"answer": "hi", "clarification": {"options": []}, "error": None})
    assert out == "hi"
    assert "CLARIFICATION" not in out


from agents.shared.eval_multiturn import group_runs_by_session  # noqa: E402

def test_group_runs_orders_turns():
    runs = {
        "s1#turn1": {"scenario_session": "s1", "turn_idx": 1, "clarified": False, "agent_sql": "SELECT 1"},
        "s1#turn0": {"scenario_session": "s1", "turn_idx": 0, "clarified": True,  "agent_sql": ""},
        "s2#turn0": {"scenario_session": "s2", "turn_idx": 0, "clarified": False, "agent_sql": "SELECT 2"},
    }
    grouped = group_runs_by_session(runs)
    assert set(grouped.keys()) == {"s1", "s2"}
    assert [t["turn_idx"] for t in grouped["s1"]] == [0, 1]   # ordered
    assert grouped["s1"][0]["clarified"] is True
    assert [t["turn_idx"] for t in grouped["s2"]] == [0]


def test_group_runs_empty_input():
    assert group_runs_by_session({}) == {}

def test_group_runs_sorts_three_turns_regardless_of_insertion_order():
    runs = {
        "s#turn2": {"scenario_session": "s", "turn_idx": 2, "agent_sql": "C"},
        "s#turn0": {"scenario_session": "s", "turn_idx": 0, "agent_sql": "A"},
        "s#turn1": {"scenario_session": "s", "turn_idx": 1, "agent_sql": "B"},
    }
    turns = group_runs_by_session(runs)["s"]
    assert [t["turn_idx"] for t in turns] == [0, 1, 2]
    assert [t["agent_sql"] for t in turns] == ["A", "B", "C"]


def test_dataset_has_simulated_scenario():
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[2] / "data/eval/groundtruth_dataset.json"
    rows = json.loads(p.read_text())
    specs = [parse_multiturn_row(r, index=i) for i, r in enumerate(rows)]
    sim = [s for s in specs if s["mode"] == "simulated"]
    assert any(s["scenario_id"] == "mt-simulated-party-count" for s in sim)
    s = next(s for s in sim if s["scenario_id"] == "mt-simulated-party-count")
    assert s["actor_profile"]["goal"]  # non-empty goal
    assert s["max_turns"] == 4
