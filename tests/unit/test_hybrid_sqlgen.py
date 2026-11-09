from agents.semantic_layer_query_agent.sql_generator import SqlGenerator
def test_generate_includes_negative_constraint():
    captured = {}
    gen = SqlGenerator(invoke_model=lambda prompt, **k: (captured.setdefault("p", prompt), "SELECT 1")[1])
    sql = gen.generate(slice_text="{}", question="q", negative_constraint="property:Address/state_tc on Party")
    assert "Do NOT use" in captured["p"] and "state_tc" in captured["p"]
    assert sql == "SELECT 1"
