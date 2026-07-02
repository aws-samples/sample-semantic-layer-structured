"""Tier 2 VKG Phase 5 — translate SPARQL→SQL (Ontop) then execute on Athena.

The Phase 5 execution closure built by ``_build_phase_deps`` no longer runs the
generated SPARQL against the schema-only Neptune graph (which returns 0
instances). It now translates the grounded SPARQL into Athena SQL via the
``translate_sparql_to_sql`` gateway tool and executes that SQL on Athena (where
the real data lives). These fakes are shared with the Task 10 repair-loop tests.
"""
from __future__ import annotations

import types


class _FakeGateway:
    """Stands in for NeptuneGatewayClient: records translate_sql, fails on run_select."""

    def __init__(self, translate):
        self._translate = translate
        self.translate_called_with_sparql = None

    def translate_sql(self, *, sparql, ontology_json, ontology_id=""):
        self.translate_called_with_sparql = sparql
        if isinstance(self._translate, Exception):
            raise self._translate
        return dict(self._translate)

    def run_select(self, *, sparql):  # Phase 5 must NOT call this anymore
        raise AssertionError(
            "Phase 5 called Neptune run_select instead of translate_sql"
        )


def test_phase5_translates_then_executes_on_athena(monkeypatch):
    from agents.ontology_query_agent import main

    gw = _FakeGateway(
        translate={
            "sql": "SELECT COUNT(*) n FROM normalized.admin_codes",
            "database": "normalized",
            "catalog": "AwsDataCatalog",
        }
    )
    monkeypatch.setattr(
        main,
        "_run_athena_sql",
        lambda **k: {
            "columns": ["n"],
            "rows": [["10"]],
            "over_limit": False,
            "state_change_reason": "",
        },
    )
    # Isolate translate/execute from the Phase-5 answer renderer (its own LLM
    # call — tested separately). Stub it to a deterministic string with no usage.
    monkeypatch.setattr(main, "_render_answer",
                        lambda **k: "The result is 10.")
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution(
        "SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }"
    )
    assert out["rows"] == [["10"]]
    assert out["columns"] == ["n"]
    assert out["answer"] == "The result is 10."
    assert gw.translate_called_with_sparql is not None  # used translate, not run_select
    # No repair ran on the success path → no repair tokens (the renderer is
    # stubbed here, so usage stays empty; the renderer's own usage is tested
    # in test_phase5_answer_renderer_*).
    assert out.get("usage") == {}


def test_phase5_counts_repair_usage(monkeypatch):
    """A repaired path must report the repair LLM's token usage (todo item 5)."""
    from agents.ontology_query_agent import main

    gw = _FakeGateway(
        translate={
            "sql": "SELECT bad",
            "database": "normalized",
            "catalog": "AwsDataCatalog",
        }
    )
    calls = {"n": 0}

    def fake_exec(**k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"columns": [], "rows": [], "state_change_reason": "SYNTAX_ERROR: line 1"}
        return {"columns": ["n"], "rows": [["10"]], "over_limit": False}

    def fake_repair(*, sql, error, ontology_json, usage_sink=None):
        # Mirror the real _repair_sql contract: fold this call's usage into the
        # caller-provided accumulator, then return the repaired SQL string.
        if usage_sink is not None:
            for key, val in {"inputTokens": 5, "outputTokens": 7,
                             "totalTokens": 12}.items():
                usage_sink[key] = usage_sink.get(key, 0) + val
        return "SELECT COUNT(*) n FROM normalized.admin_codes"

    monkeypatch.setattr(main, "_run_athena_sql", fake_exec)
    monkeypatch.setattr(main, "_repair_sql", fake_repair)
    # Isolate repair-usage accounting from the answer renderer (stubbed, no usage).
    monkeypatch.setattr(main, "_render_answer", lambda **k: "ok")
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution(
        "SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }"
    )
    assert out["rows"] == [["10"]]
    assert out["usage"].get("totalTokens") == 12
    assert out["usage"].get("inputTokens") == 5
    assert out["usage"].get("outputTokens") == 7


class _SeqGateway:
    """Gateway whose translate_sql returns scripted results in sequence.

    Each call pops the next entry from ``results`` (a list of dicts or
    Exceptions). Records every SPARQL it was asked to translate so a test can
    assert the repaired SPARQL was re-submitted.
    """

    def __init__(self, results):
        self._results = list(results)
        self.sparqls = []
        self.calls = 0

    def translate_sql(self, *, sparql, ontology_json, ontology_id=""):
        self.sparqls.append(sparql)
        self.calls += 1
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return dict(item)

    def run_select(self, *, sparql):
        raise AssertionError("Phase 5 called run_select instead of translate_sql")


def test_phase5_repairs_sparql_translation_once_then_succeeds(monkeypatch):
    """A first translate FAILURE ({"error"}) triggers one SPARQL repair, and the
    repaired SPARQL is re-translated successfully (Fix 1)."""
    from agents.ontology_query_agent import main

    gw = _SeqGateway([
        {"error": "It is not possible to reuse the projection alias ?month in GROUP BY"},
        {"sql": "SELECT month, SUM(amt) FROM normalized.financial_activity GROUP BY month",
         "database": "normalized", "catalog": "AwsDataCatalog"},
    ])
    monkeypatch.setattr(main, "_run_athena_sql",
                        lambda **k: {"columns": ["month", "_col1"],
                                     "rows": [["2024-01", "100"]],
                                     "over_limit": False, "state_change_reason": ""})
    monkeypatch.setattr(main, "_render_answer", lambda **k: "Jan total was 100.")

    def fake_sparql_repair(*, sparql, error, usage_sink=None):
        if usage_sink is not None:
            for key, val in {"inputTokens": 3, "outputTokens": 9,
                             "totalTokens": 12}.items():
                usage_sink[key] = usage_sink.get(key, 0) + val
        return "SELECT (SUBSTR(?d,1,7) AS ?month) (SUM(?amt) AS ?t) WHERE { ?x a <http://x/FA> } GROUP BY (SUBSTR(?d,1,7))"

    monkeypatch.setattr(main, "_repair_sparql_for_translation", fake_sparql_repair)
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution("SELECT (SUBSTR(?d,1,7) AS ?month) ... GROUP BY ?month")
    assert out["rows"] == [["2024-01", "100"]]
    assert gw.calls == 2  # original failed, repaired re-translated
    assert gw.sparqls[1].startswith("SELECT (SUBSTR")  # repaired SPARQL re-submitted
    # The translation-repair tokens are folded into the returned usage.
    assert out["usage"].get("totalTokens") == 12


def test_phase5_degrades_when_sparql_translation_repair_exhausted(monkeypatch):
    """If the repaired SPARQL ALSO fails to translate, degrade after 2 attempts."""
    from agents.ontology_query_agent import main

    gw = _SeqGateway([
        {"error": "Ontop translation failed"},
        {"error": "Ontop translation failed again"},
    ])
    monkeypatch.setattr(
        main, "_run_athena_sql",
        lambda **k: (_ for _ in ()).throw(AssertionError("should not run Athena")),
    )
    monkeypatch.setattr(main, "_repair_sparql_for_translation",
                        lambda **k: "SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/C> }")
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution("SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/C> }")
    assert out["degraded"] == "sparql_translation_failed"
    assert gw.calls == 2  # original + one repaired retry, then give up
    assert out["rows"] == []


def test_phase5_degrades_when_sparql_translation_repair_returns_empty(monkeypatch):
    """A blank SPARQL repair must degrade WITHOUT a second translate call."""
    from agents.ontology_query_agent import main

    gw = _SeqGateway([{"error": "Ontop translation failed"}])
    monkeypatch.setattr(
        main, "_run_athena_sql",
        lambda **k: (_ for _ in ()).throw(AssertionError("should not run Athena")),
    )
    monkeypatch.setattr(main, "_repair_sparql_for_translation", lambda **k: "   ")
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution("SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/C> }")
    assert out["degraded"] == "sparql_translation_failed"
    assert gw.calls == 1  # blank repair short-circuits before re-translating


def test_phase5_degrades_when_sparql_translation_repair_raises(monkeypatch):
    """A raised SPARQL-repair Bedrock call degrades, not crashes."""
    from agents.ontology_query_agent import main

    gw = _SeqGateway([{"error": "Ontop translation failed"}])
    monkeypatch.setattr(
        main, "_run_athena_sql",
        lambda **k: (_ for _ in ()).throw(AssertionError("should not run Athena")),
    )

    def _boom(**k):
        raise RuntimeError("ThrottlingException")

    monkeypatch.setattr(main, "_repair_sparql_for_translation", _boom)
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution("SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/C> }")
    assert out["degraded"] == "sparql_translation_failed"
    assert gw.calls == 1


def test_repair_sparql_for_translation_strips_fences(monkeypatch):
    """The REAL _repair_sparql_for_translation strips fences + embeds error+SPARQL."""
    main = _patch_repair_agent(
        monkeypatch, content=[{"text": "```sparql\nSELECT ?x WHERE { ?x a <http://x/C> }\n```"}],
    )
    out = main._repair_sparql_for_translation(
        sparql="SELECT (X AS ?x) WHERE {} GROUP BY ?x",
        error="alias reuse in GROUP BY",
    )
    assert out == "SELECT ?x WHERE { ?x a <http://x/C> }"  # fence stripped
    assert "alias reuse in GROUP BY" in _FakeRepairAgent.last_prompt
    assert "GROUP BY ?x" in _FakeRepairAgent.last_prompt


def test_repair_sparql_for_translation_empty_on_malformed(monkeypatch):
    """A degenerate completion yields "" and does NOT raise."""
    main = _patch_repair_agent(monkeypatch, content=[])
    out = main._repair_sparql_for_translation(sparql="SELECT bad", error="err")
    assert out == ""


def test_phase5_degrades_when_translate_raises(monkeypatch):
    """A raised translate_sql (e.g. non-JSON gateway body) degrades, not crashes."""
    from agents.ontology_query_agent import main

    gw = _FakeGateway(translate=RuntimeError("translate_sparql_to_sql returned non-JSON"))
    # _run_athena_sql must never be reached when translation raises.
    monkeypatch.setattr(
        main, "_run_athena_sql",
        lambda **k: (_ for _ in ()).throw(AssertionError("should not run Athena")),
    )
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution("SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }")
    assert out["degraded"] == "sparql_translation_failed"
    assert out["rows"] == []
    assert out["columns"] == []
    assert out["sql"] == ""


def test_phase5_degrades_when_athena_execution_raises(monkeypatch):
    """A raised _run_athena_sql (boto3 infra ClientError) degrades, not crashes."""
    from agents.ontology_query_agent import main

    gw = _FakeGateway(
        translate={
            "sql": "SELECT COUNT(*) n FROM normalized.admin_codes",
            "database": "normalized",
            "catalog": "AwsDataCatalog",
        }
    )
    monkeypatch.setattr(
        main, "_run_athena_sql",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boto3 ClientError: AccessDenied")),
    )
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution("SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }")
    assert out["degraded"] == "sql_execution_failed"
    assert out["rows"] == []
    assert out["columns"] == []
    # the translated SQL is preserved on the degraded dict for Task 10/11.
    assert out["sql"] == "SELECT COUNT(*) n FROM normalized.admin_codes"


def test_phase5_repairs_sql_once_then_succeeds(monkeypatch):
    from agents.ontology_query_agent import main
    gw = _FakeGateway(translate={"sql":"SELECT bad","database":"normalized","catalog":"AwsDataCatalog"})
    calls = {"n":0}
    def fake_exec(**k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"columns":[],"rows":[],"state_change_reason":"SYNTAX_ERROR: line 1","over_limit":False}
        return {"columns":["n"],"rows":[["10"]],"over_limit":False,"state_change_reason":""}
    monkeypatch.setattr(main, "_run_athena_sql", fake_exec)
    monkeypatch.setattr(main, "_repair_sql", lambda **k: "SELECT COUNT(*) n FROM normalized.admin_codes")
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings":{}})
    out = deps.run_execution("SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }")
    assert out["rows"] == [["10"]] and calls["n"] == 2


def test_phase5_degrades_when_repair_exhausted(monkeypatch):
    from agents.ontology_query_agent import main
    gw = _FakeGateway(translate={"sql":"SELECT bad","database":"d","catalog":"c"})
    monkeypatch.setattr(main, "_run_athena_sql",
        lambda **k: {"columns":[],"rows":[],"state_change_reason":"SYNTAX_ERROR","over_limit":False})
    monkeypatch.setattr(main, "_repair_sql", lambda **k: "SELECT still bad")
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings":{}})
    out = deps.run_execution("SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }")
    assert out["degraded"] == "sql_execution_failed"


# --- Task 10 code-review FIX A: a raised _repair_sql must degrade, not crash. ---


def test_phase5_degrades_when_repair_raises(monkeypatch):
    """A live Bedrock repair call that RAISES (throttle/timeout) must degrade.

    The repair call sits outside the _run_athena_sql try/except, so a raise
    there would otherwise escape _run_execution and crash the graph node.
    """
    from agents.ontology_query_agent import main
    gw = _FakeGateway(translate={"sql": "SELECT bad", "database": "d", "catalog": "c"})
    # Athena keeps reporting a query failure (non-raising) so the loop reaches repair.
    monkeypatch.setattr(
        main, "_run_athena_sql",
        lambda **k: {"columns": [], "rows": [], "state_change_reason": "SYNTAX_ERROR",
                     "over_limit": False},
    )

    def _boom(**k):
        raise RuntimeError("ThrottlingException: Too many requests")

    monkeypatch.setattr(main, "_repair_sql", _boom)
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution("SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }")
    assert out["degraded"] == "sql_execution_failed"
    assert out["rows"] == []
    # The last good SQL is preserved on the degraded dict.
    assert out["sql"] == "SELECT bad"


def test_phase5_degrades_when_repair_returns_empty(monkeypatch):
    """An empty/blank repaired SQL must degrade WITHOUT a second Athena re-exec."""
    from agents.ontology_query_agent import main
    gw = _FakeGateway(translate={"sql": "SELECT bad", "database": "d", "catalog": "c"})
    calls = {"n": 0}

    def fake_exec(**k):
        calls["n"] += 1
        return {"columns": [], "rows": [], "state_change_reason": "SYNTAX_ERROR",
                "over_limit": False}

    monkeypatch.setattr(main, "_run_athena_sql", fake_exec)
    monkeypatch.setattr(main, "_repair_sql", lambda **k: "   ")  # blank repair
    deps = main._build_phase_deps(gateway=gw, ontology_json={"mappings": {}})
    out = deps.run_execution("SELECT (COUNT(?a) AS ?n) WHERE { ?a a <http://x/AdminCode> }")
    assert out["degraded"] == "sql_execution_failed"
    # Only ONE exec — the blank repair short-circuits before re-running empty SQL.
    assert calls["n"] == 1


# --- Task 10 code-review FIX D: directly unit-test the REAL _repair_sql body. ---


class _FakeRepairAgent:
    """Strands-style Agent stand-in: records the prompt, returns a fixed result.

    Mirrors how the real code reads ``result.message["content"][0]["text"]`` —
    the call returns an object whose ``.message`` is a dict containing
    ``{"content": [{"text": "..."}]}``.
    """

    last_prompt: str = ""

    def __init__(self, *, content):
        self._content = content

    def __call__(self, prompt):
        type(self).last_prompt = prompt
        return types.SimpleNamespace(message={"content": self._content})


def _patch_repair_agent(monkeypatch, *, content):
    """Patch main.Agent + main._build_query_model so _repair_sql uses the fake."""
    from agents.ontology_query_agent import main
    _FakeRepairAgent.last_prompt = ""
    # _build_query_model would make a real Bedrock client; stub it out.
    monkeypatch.setattr(main, "_build_query_model", lambda: object())
    monkeypatch.setattr(main, "Agent", lambda **kw: _FakeRepairAgent(content=content))
    return main


def test_repair_sql_strips_fences(monkeypatch):
    """The REAL _repair_sql strips ```sql fences and embeds the error + SQL."""
    main = _patch_repair_agent(
        monkeypatch, content=[{"text": "```sql\nSELECT 1\n```"}],
    )
    out = main._repair_sql(
        sql="SELCT 1", error="SYNTAX_ERROR: mismatched input 'SELCT'",
        ontology_json={"mappings": {}},
    )
    assert out == "SELECT 1"  # fence stripped
    # The prompt the fake received contains the error string + the failing SQL.
    assert "SYNTAX_ERROR: mismatched input 'SELCT'" in _FakeRepairAgent.last_prompt
    assert "SELCT 1" in _FakeRepairAgent.last_prompt


def test_repair_sql_returns_empty_on_malformed_result(monkeypatch):
    """A degenerate completion (empty content) yields "" and does NOT raise."""
    main = _patch_repair_agent(monkeypatch, content=[])
    out = main._repair_sql(
        sql="SELECT bad", error="SYNTAX_ERROR", ontology_json={"mappings": {}},
    )
    assert out == ""


def test_repair_sql_returns_empty_on_tool_use_only_result(monkeypatch):
    """A tool-use-only block (no ``text`` key) yields "" and does NOT raise."""
    main = _patch_repair_agent(
        monkeypatch, content=[{"toolUse": {"name": "x", "input": {}}}],
    )
    out = main._repair_sql(
        sql="SELECT bad", error="SYNTAX_ERROR", ontology_json={"mappings": {}},
    )
    assert out == ""


def test_phase5_answer_renderer_produces_llm_answer_and_counts_usage(monkeypatch):
    """The Phase-5 answer renderer turns the Athena result into the NL answer via
    a REAL bounded LLM call (so the SDK emits an in-graph chat span the eval
    harvester captures), embeds the question + result rows in the prompt, and
    folds its token usage into the supplied sink."""
    main = _patch_repair_agent(monkeypatch, content=[{"text": "There are 15 parties."}])

    # The fake Agent has no .metrics, so _extract_usage_summary returns {}. Stub
    # it to a fixed usage so we can assert the sink accumulation.
    monkeypatch.setattr(main, "_extract_usage_summary",
                        lambda result: {"inputTokens": 11, "outputTokens": 4,
                                        "totalTokens": 15})
    sink: dict = {}
    answer = main._render_answer(
        question="How many parties are there?",
        columns=["n"], rows=[["15"]], over_limit=False, usage_sink=sink,
    )
    assert answer == "There are 15 parties."
    # Question + result rows reached the renderer prompt (the parity change).
    assert "How many parties are there?" in _FakeRepairAgent.last_prompt
    assert "15" in _FakeRepairAgent.last_prompt
    # Real renderer usage was counted.
    assert sink == {"inputTokens": 11, "outputTokens": 4, "totalTokens": 15}


def test_build_domain_context_from_config():
    """_build_domain_context stitches name + useCases/dataSources into one line."""
    from agents.ontology_query_agent import main
    ctx = main._build_domain_context({
        "name": "Curated Insurance Layer",
        "useCasesDescription": "life insurance and annuity policy analytics",
        "dataSourcesDescription": "ACORD-derived party/policy/coverage tables",
    })
    assert ctx.startswith("DOMAIN CONTEXT:")
    assert "Curated Insurance Layer" in ctx
    assert "annuity" in ctx
    assert "never a generic/world-knowledge sense" in ctx


def test_build_domain_context_empty_when_no_description():
    """No description fields → empty string (prompts read unchanged)."""
    from agents.ontology_query_agent import main
    assert main._build_domain_context({}) == ""
    assert main._build_domain_context({"useCasesDescription": "   "}) == ""


def test_render_answer_injects_domain_context(monkeypatch):
    """The domain descriptor is prepended to the answer system prompt (Fix 2)."""
    from agents.ontology_query_agent import main

    captured = {}

    class _Agent:
        def __init__(self, *, model, system_prompt, tools):
            captured["system_prompt"] = system_prompt

        def __call__(self, prompt):
            return types.SimpleNamespace(message={"content": [{"text": "There are 15 parties."}]})

    monkeypatch.setattr(main, "_build_query_model", lambda: object())
    monkeypatch.setattr(main, "Agent", _Agent)
    monkeypatch.setattr(main, "_extract_usage_summary", lambda r: {})
    out = main._render_answer(
        question="how many parties", columns=["n"], rows=[["15"]],
        over_limit=False, usage_sink={},
        domain_context="DOMAIN CONTEXT: this is an insurance layer.",
    )
    assert out == "There are 15 parties."
    # The domain line precedes the standard answer prompt.
    assert captured["system_prompt"].startswith("DOMAIN CONTEXT: this is an insurance layer.")
    assert "report the result" in captured["system_prompt"].lower()


def test_phase5_answer_renderer_falls_back_to_deterministic_on_error(monkeypatch):
    """If the renderer LLM raises, the answer falls back to the deterministic
    _summarize_select — a render failure must never break the answer."""
    from agents.ontology_query_agent import main

    monkeypatch.setattr(main, "_build_query_model", lambda: object())

    def _boom(**kw):
        raise RuntimeError("bedrock throttled")

    monkeypatch.setattr(main, "Agent", _boom)
    answer = main._render_answer(
        question="how many parties", columns=["n"], rows=[["15"]],
        over_limit=False, usage_sink={},
    )
    # Deterministic scalar fallback.
    assert answer == "The result is 15."
