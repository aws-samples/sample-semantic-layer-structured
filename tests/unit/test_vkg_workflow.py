"""Unit tests for the VKG Tier 2 Strands graph workflow (tier2/workflow.py).

These drive the REAL Strands Graph engine (conftest loads the genuine
strands.multiagent submodules) with stubbed phase dependencies, so they
exercise the actual node ordering, conditional edges, and the §0.1 HYBRID
grounding back-edge (expand → Phase 3 / regenerate → Phase 4).
"""
from agents.ontology_query_agent.tier2.workflow import (
    PhaseDeps,
    tier2_vkg_workflow,
)

EX = "http://ex.com/"


def _slice_ttl(body: str) -> str:
    return (f"@prefix ex: <{EX}> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            f"{body}")


# A slice with Policy + hasPremium(domain Policy). Grounds
# "SELECT ?x WHERE { ?x a ex:Policy . ?x ex:hasPremium ?p }".
_POLICY_SLICE = _slice_ttl(
    "ex:Policy a rdfs:Class . ex:hasPremium rdfs:domain ex:Policy ."
)


class _Router:
    def __init__(self, candidates):
        self._candidates = candidates

    def find_candidates(self, *, question, namespace):
        return list(self._candidates)


class _Builder:
    """Slice builder stub. ``slice_for`` may be a string or a callable
    ``(candidates) -> ttl`` so an expand round can return a wider slice."""

    def __init__(self, slice_for, sufficient=True):
        self._slice_for = slice_for
        self._sufficient = sufficient
        self.judge_usage = {"inputTokens": 1, "outputTokens": 2, "totalTokens": 3}

    def build(self, *, candidates, namespace):
        if callable(self._slice_for):
            return self._slice_for(candidates)
        return self._slice_for

    def is_sufficient(self, *, slice_text, question):
        return self._sufficient, None

    def expand(self, *, slice_text, missing):
        return slice_text


class _Gen:
    """SPARQL generator stub.

    ``sparql`` may be a string or a callable ``(grounding_feedback) -> str``
    so a test can model regeneration after the grounding gate feeds back.
    """

    def __init__(self, sparql):
        self._sparql = sparql
        self.last_usage = {"inputTokens": 4, "outputTokens": 5, "totalTokens": 9}

    def generate(self, *, slice_text, question, grounding_feedback=""):
        if callable(self._sparql):
            return self._sparql(grounding_feedback)
        return self._sparql


def _events_collector():
    events = []

    def sink(phase, action, payload):
        events.append((phase, action, payload.get("step")))

    return events, sink


def test_happy_path_runs_all_phases_in_order():
    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_Builder(_POLICY_SLICE),
        generator=_Gen(f"PREFIX ex: <{EX}> SELECT ?x WHERE "
                       "{ ?x a ex:Policy . ?x ex:hasPremium ?p }"),
        run_execution=lambda sparql, **_kw: {"columns": ["x"], "rows": [["1"]],
                                      "answer": "1 policy", "n_quads": ["q"],
                                      "usage": {"inputTokens": 7,
                                                "outputTokens": 8,
                                                "totalTokens": 15}},
    )
    events, sink = _events_collector()
    ctx = tier2_vkg_workflow(question="policies premium", namespace="ns",
                             deps=deps, phase_sink=sink)
    assert ctx.degraded is None
    assert ctx.needs_clarification is None
    assert ctx.execution_result["rows"] == [["1"]]
    # usage accumulated from judge (3) + generator (9) + execution (15) totals.
    assert ctx.usage["totalTokens"] == 3 + 9 + 15
    starts = [(p, step) for (p, a, step) in events if a == "phase_start"]
    assert (1, None) in starts and (2, None) in starts
    assert (3, None) in starts and (3, "3b") in starts
    assert (4, None) in starts and (5, None) in starts


def test_grounding_span_emitted_with_slice_and_sql_on_success(monkeypatch):
    # VKG Phase 5 is deterministic (no LLM call), so the SDK emits no harvested
    # span and the SESSION-level SqlGrounded judge would have no ontology slice
    # to verify the executed SQL against. The Phase 5 node must call
    # emit_grounding_span on the successful path with the slice (input) + the
    # executed Athena SQL (output) so the judge can ground.
    import agents.ontology_query_agent.tier2.workflow as wf

    captured = {}

    def _fake_emit(*, retrieved_schema, executed_sql, question=""):
        captured["schema"] = retrieved_schema
        captured["sql"] = executed_sql
        captured["question"] = question

    monkeypatch.setattr(wf, "emit_grounding_span", _fake_emit)

    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_Builder(_POLICY_SLICE),
        generator=_Gen(f"PREFIX ex: <{EX}> SELECT ?x WHERE "
                       "{ ?x a ex:Policy . ?x ex:hasPremium ?p }"),
        run_execution=lambda sparql, **_kw: {"columns": ["x"], "rows": [["1"]],
                                      "answer": "1 policy", "n_quads": [],
                                      "sql": "SELECT x FROM policy"},
    )
    ctx = tier2_vkg_workflow(question="policies premium", namespace="ns",
                             deps=deps)
    assert ctx.degraded is None
    # The slice (Turtle) reached the span as grounding context, and the executed
    # Athena SQL (from the execution result) reached it as the output.
    assert captured.get("schema") == _POLICY_SLICE
    assert captured.get("sql") == "SELECT x FROM policy"
    assert captured.get("question") == "policies premium"


def test_grounding_span_not_emitted_on_degraded_execution(monkeypatch):
    # When Phase 5 execution degrades (e.g. SQL translation/execution failed),
    # there is no grounded SQL to record — the span must NOT be emitted.
    import agents.ontology_query_agent.tier2.workflow as wf

    calls = {"n": 0}
    monkeypatch.setattr(wf, "emit_grounding_span",
                        lambda **_kw: calls.__setitem__("n", calls["n"] + 1))

    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_Builder(_POLICY_SLICE),
        generator=_Gen(f"PREFIX ex: <{EX}> SELECT ?x WHERE "
                       "{ ?x a ex:Policy . ?x ex:hasPremium ?p }"),
        run_execution=lambda sparql, **_kw: {"columns": [], "rows": [], "n_quads": [],
                                      "degraded": "sql_execution_failed",
                                      "answer": "failed", "sql": "SELECT 1"},
    )
    ctx = tier2_vkg_workflow(question="policies", namespace="ns", deps=deps)
    assert ctx.degraded == "sql_execution_failed"
    assert calls["n"] == 0, "grounding span must not be emitted on a degraded execution"


def test_phase3_insufficient_slice_short_circuits_to_degraded():
    # When the judge never reaches sufficiency within MAX_PHASE3_ROUNDS, the
    # graph must degrade (phase3_max_rounds) and NOT generate/execute SPARQL
    # against the rejected slice — that previously produced a misleading 0-row
    # answer (the screenshotted failure).
    runs = {"exec": 0, "gen": 0}

    class _NeverSufficient(_Builder):
        def is_sufficient(self, *, slice_text, question):
            return False, ["ex:MissingThing"]

    class _CountingGen(_Gen):
        def generate(self, *, slice_text, question, grounding_feedback=""):
            runs["gen"] += 1
            return super().generate(slice_text=slice_text, question=question,
                                    grounding_feedback=grounding_feedback)

    def _exec(sparql, **_kw):
        runs["exec"] += 1
        return {"columns": [], "rows": []}

    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_NeverSufficient(_POLICY_SLICE),
        generator=_CountingGen("SELECT ?x WHERE { ?x a ex:Policy }"),
        run_execution=_exec,
    )
    payloads = []
    ctx = tier2_vkg_workflow(question="policies", namespace="ns", deps=deps,
                             phase_sink=lambda ph, a, pl: payloads.append((ph, a, pl)))
    assert ctx.degraded == "phase3_max_rounds"
    assert runs["gen"] == 0, "Phase 4 must not generate SPARQL on an insufficient slice"
    assert runs["exec"] == 0, "Phase 5 must not execute on an insufficient slice"
    assert ctx.execution_result == {}
    # The phase_result must carry per-round judge diagnostics so a degrade is
    # explainable (one record per round, each flagged insufficient with the
    # judge's `missing` list).
    p3 = [pl for (ph, a, pl) in payloads
          if ph == 3 and a == "phase_result" and pl.get("step") is None]
    assert p3, "expected a Phase 3 phase_result"
    detail = p3[0].get("judgeRoundsDetail")
    assert isinstance(detail, list) and len(detail) >= 1
    assert all(r["sufficient"] is False for r in detail)
    assert detail[0]["missing"] == ["ex:MissingThing"]
    assert "sliceTokens" in detail[0]


def test_phase3_overrides_judge_false_negative_when_missing_is_present():
    # Self-contradiction override: the judge says insufficient and names
    # ex:Policy as missing, but ex:Policy IS in the slice (_POLICY_SLICE). The
    # deterministic guard must trust the slice, proceed to Phase 4/5, and NOT
    # degrade. (Reproduces the deployed VKG false-negative on CoverageProduct.)
    runs = {"exec": 0, "gen": 0}

    class _FalseNegative(_Builder):
        def is_sufficient(self, *, slice_text, question):
            return False, [f"{EX}Policy"]  # present in _POLICY_SLICE

    class _CountingGen(_Gen):
        def generate(self, *, slice_text, question, grounding_feedback=""):
            runs["gen"] += 1
            return super().generate(slice_text=slice_text, question=question,
                                    grounding_feedback=grounding_feedback)

    def _exec(sparql, **_kw):
        runs["exec"] += 1
        return {"columns": ["x"], "rows": [["1"]], "answer": "1", "n_quads": ["q"]}

    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_FalseNegative(_POLICY_SLICE),
        generator=_CountingGen("SELECT ?x WHERE { ?x a ex:Policy }"),
        run_execution=_exec,
    )
    ctx = tier2_vkg_workflow(question="policies", namespace="ns", deps=deps)
    assert ctx.degraded != "phase3_max_rounds", "present-IRI false-negative must not degrade"
    assert runs["gen"] == 1, "Phase 4 must run once the slice is trusted"
    assert runs["exec"] == 1, "Phase 5 must execute the trusted slice"


def test_phase3_override_fuzzy_matches_misspelled_property():
    # The judge fabricates a near-miss property name (partyTypeTc) for a property
    # that really exists as party_type_code in the slice. The fuzzy tier of the
    # presence check must recognise it (partytype == partytype) and override, so
    # an answerable party-type question is not blocked by the judge's mis-spelling.
    slice_ttl = _slice_ttl(
        "ex:Party a rdfs:Class . ex:Party/party_type_code rdfs:domain ex:Party ."
    )
    runs = {"gen": 0}

    class _Misspell(_Builder):
        def is_sufficient(self, *, slice_text, question):
            return False, [f"{EX}Party", f"{EX}Party/partyTypeTc"]

    class _CountingGen(_Gen):
        def generate(self, *, slice_text, question, grounding_feedback=""):
            runs["gen"] += 1
            return super().generate(slice_text=slice_text, question=question,
                                    grounding_feedback=grounding_feedback)

    deps = PhaseDeps(
        router=_Router([f"{EX}Party"]),
        builder=_Misspell(slice_ttl),
        generator=_CountingGen("SELECT ?x WHERE { ?x a ex:Party }"),
        run_execution=lambda sparql, **_kw: {"columns": ["x"], "rows": [["1"]],
                                      "answer": "1", "n_quads": ["q"]},
    )
    ctx = tier2_vkg_workflow(question="party types", namespace="ns", deps=deps)
    assert ctx.degraded != "phase3_max_rounds", "misspelled-but-real property must override"
    assert runs["gen"] == 1


def test_phase3_genuinely_absent_missing_still_degrades():
    # Contrast: a missing IRI that is genuinely ABSENT from the slice must still
    # degrade — the override only fires when EVERY missing IRI is present.
    class _RealGap(_Builder):
        def is_sufficient(self, *, slice_text, question):
            return False, [f"{EX}NotInSliceAtAll"]

    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_RealGap(_POLICY_SLICE),
        generator=_Gen("SELECT ?x WHERE { ?x a ex:Policy }"),
        run_execution=lambda sparql, **_kw: {"columns": [], "rows": []},
    )
    ctx = tier2_vkg_workflow(question="policies", namespace="ns", deps=deps)
    assert ctx.degraded == "phase3_max_rounds"


def test_phase3_start_and_result_share_round_key():
    """Regression: Phase 3 phase_start and phase_result must carry the SAME
    round, even when the judge loop runs multiple internal iterations — else the
    frontend (which keys rows by phase:step:round) orphans the start row at
    '...' and the result lands on a new row."""
    # Builder insufficient on round 1, sufficient after one expand → 2 judge
    # iterations inside a single Phase-3 visit.
    calls = {"n": 0}

    class _TwoRoundBuilder(_Builder):
        def is_sufficient(self, *, slice_text, question):
            calls["n"] += 1
            return (calls["n"] >= 2), (["x"] if calls["n"] < 2 else None)

    rounds_seen = {"start": [], "result": []}

    def sink(phase, action, payload):
        if phase == 3 and payload.get("step") is None:
            rounds_seen[action.replace("phase_", "")].append(payload.get("round"))

    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_TwoRoundBuilder(_POLICY_SLICE),
        generator=_Gen(f"PREFIX ex: <{EX}> SELECT ?x WHERE "
                       "{ ?x a ex:Policy . ?x ex:hasPremium ?p }"),
        run_execution=lambda sparql, **_kw: {"columns": ["x"], "rows": [["1"]],
                                      "answer": "ok", "n_quads": []},
    )
    tier2_vkg_workflow(question="policies", namespace="ns", deps=deps,
                       phase_sink=sink)
    # Exactly one start + one result for the Phase-3 visit, and same round.
    assert rounds_seen["start"] == [1]
    assert rounds_seen["result"] == [1]


def test_empty_candidates_routes_to_degraded():
    deps = PhaseDeps(
        router=_Router([]),
        builder=_Builder(_POLICY_SLICE),
        generator=_Gen("SELECT 1"),
        run_execution=lambda sparql, **_kw: {"rows": []},
    )
    ctx = tier2_vkg_workflow(question="x", namespace="ns", deps=deps)
    assert ctx.degraded == "phase1_empty"
    assert ctx.execution_result == {}


def test_hallucinated_predicate_regenerates_then_succeeds():
    # Round 1 uses ex:hasNonsense (not in slice, not a candidate) → regenerate
    # back to Phase 4 → clean SPARQL grounds and executes. Slice NOT widened.
    runs = {"n": 0}

    def gen(feedback):
        return (f"PREFIX ex: <{EX}> SELECT ?x WHERE "
                "{ ?x a ex:Policy . ?x ex:hasPremium ?p }" if feedback
                else f"PREFIX ex: <{EX}> SELECT ?x WHERE "
                "{ ?x a ex:Policy . ?x ex:hasNonsense ?p }")

    def run_exec(sparql, **_kw):
        runs["n"] += 1
        return {"columns": ["x"], "rows": [["1"]], "answer": "ok"}

    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_Builder(_POLICY_SLICE),
        generator=_Gen(gen),
        run_execution=run_exec,
    )
    ctx = tier2_vkg_workflow(question="policies", namespace="ns", deps=deps)
    assert ctx.grounding_rounds == 1
    assert ctx.degraded is None
    assert ctx.grounding_feedback  # hallucinated IRI captured as negative constraint
    assert runs["n"] == 1  # executed once, after regeneration grounded


def test_out_of_slice_predicate_expands_then_succeeds():
    # Round 1: the n_hops-bounded slice has only Policy (no hasPremium edge yet);
    # SPARQL uses ex:hasPremium which IS a Phase-1 candidate but NOT in the
    # initial slice → expand back to Phase 3. The re-build (post-expand) returns
    # the wider slice that includes hasPremium's domain → grounds.
    runs = {"n": 0}
    builds = {"n": 0}

    class _NarrowThenWideBuilder(_Builder):
        def build(self, *, candidates, namespace):
            builds["n"] += 1
            # First build = narrow (n_hops didn't reach hasPremium); the expand
            # back-edge re-invokes build → return the full slice.
            return (_slice_ttl("ex:Policy a rdfs:Class .") if builds["n"] == 1
                    else _POLICY_SLICE)

    def run_exec(sparql, **_kw):
        runs["n"] += 1
        return {"columns": ["x"], "rows": [["1"]], "answer": "ok"}

    deps = PhaseDeps(
        router=_Router([f"{EX}Policy", f"{EX}hasPremium"]),
        builder=_NarrowThenWideBuilder(None),
        generator=_Gen(f"PREFIX ex: <{EX}> SELECT ?x WHERE "
                       "{ ?x a ex:Policy . ?x ex:hasPremium ?p }"),
        run_execution=run_exec,
    )
    ctx = tier2_vkg_workflow(question="policies premium", namespace="ns", deps=deps)
    assert ctx.grounding_rounds == 1
    assert ctx.degraded is None
    assert runs["n"] == 1
    assert f"{EX}hasPremium" in ctx.candidates  # slice was widened


def test_grounding_ceiling_degrades_without_executing():
    # SPARQL keeps using a hallucinated predicate every round → never grounds →
    # ceiling hit → degrade without ever executing.
    runs = {"n": 0}

    def run_exec(sparql, **_kw):
        runs["n"] += 1
        return {"rows": []}

    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_Builder(_POLICY_SLICE),
        generator=_Gen(f"PREFIX ex: <{EX}> SELECT ?x WHERE "
                       "{ ?x a ex:Policy . ?x ex:hasNonsense ?p }"),
        run_execution=run_exec,
    )
    ctx = tier2_vkg_workflow(question="x", namespace="ns", deps=deps)
    assert ctx.degraded == "grounding_unresolved"
    assert runs["n"] == 0


def test_invoke_maps_new_degraded_states():
    """Task 11: ``_degraded_answer`` returns a clean user-facing message for the
    two NEW Phase-5 failure modes (translation / execution)."""
    from agents.ontology_query_agent import main
    for state, frag in [("sparql_translation_failed", "translate"),
                        ("sql_execution_failed", "execute")]:
        text = main._degraded_answer(state)
        assert frag in text.lower()


def test_degraded_answer_preserves_existing_states():
    """Task 11: the existing degraded messages are preserved verbatim when
    centralized in ``_degraded_answer``."""
    from agents.ontology_query_agent import main
    assert main._degraded_answer("phase1_empty") == (
        "I couldn't find any ontology classes or properties relevant to "
        "your question."
    )
    assert main._degraded_answer("sparql_repair_failed") == (
        "I was unable to construct a valid SPARQL query for your question."
    )
    assert main._degraded_answer("grounding_unresolved") == (
        "I couldn't build a query fully grounded in the available "
        "ontology for your question."
    )


def test_phase5_execution_degraded_propagates_to_ctx():
    """Task 11: a degraded execution result (translation/execution failure)
    propagates ``execution_result['degraded']`` onto ``ctx.degraded`` so
    ``invoke()`` can map it to a clean answer."""
    deps = PhaseDeps(
        router=_Router([f"{EX}Policy"]),
        builder=_Builder(_POLICY_SLICE),
        generator=_Gen(f"PREFIX ex: <{EX}> SELECT ?x WHERE "
                       "{ ?x a ex:Policy . ?x ex:hasPremium ?p }"),
        run_execution=lambda sparql, **_kw: {"columns": [], "rows": [], "n_quads": [],
                                      "degraded": "sql_execution_failed",
                                      "answer": "I ran the query but it failed "
                                                "to execute.",
                                      "usage": {}, "sql": "SELECT COUNT(*)"},
    )
    ctx = tier2_vkg_workflow(question="policies premium", namespace="ns",
                             deps=deps)
    assert ctx.degraded == "sql_execution_failed"
    # The executed SQL is preserved on the execution result for invoke() to
    # surface in reasoning.sqlQuery.
    assert ctx.execution_result["sql"] == "SELECT COUNT(*)"


def test_phase2_email_resolves_to_class_not_clarification():
    """Regression: 'email' substring-matches the class EmailAddress AND several
    of its own properties (emailType, alternateEmail). That is NOT a genuine
    ambiguity — Phase 2 must resolve to the EmailAddress class instead of asking
    'Which interpretation of email do you mean?'."""
    cls = f"{EX}EmailAddress"
    candidates = [
        cls,
        f"{EX}EmailAddress/emailType",
        f"{EX}EmailAddress/alternateEmail",
        f"{EX}EmailAddress/emailSk",
        f"{EX}Party",
    ]
    slice_ttl = _slice_ttl("ex:EmailAddress a rdfs:Class .")
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(slice_ttl),
        generator=_Gen("SELECT ?x WHERE { ?x a ex:EmailAddress }"),
        run_execution=lambda sparql, **_kw: {"columns": ["x"], "rows": [["1"]],
                                      "answer": "1 row"},
    )
    ctx = tier2_vkg_workflow(question="which parties have email addresses",
                             namespace="ns", deps=deps)
    assert ctx.needs_clarification is None, ctx.needs_clarification
    # The email concept resolved to the EmailAddress CLASS (not a property, not
    # ambiguous) — via either the "email addresses" compound or the "email" term.
    resolved_iris = {b.get("iri") for b in ctx.disambiguation.values()
                     if isinstance(b, dict)}
    assert cls in resolved_iris, ctx.disambiguation


def test_phase2_two_distinct_classes_still_clarifies():
    """Guard: a term matching TWO genuine classes (no class/property nesting)
    must still clarify — the narrowing must not over-resolve real ambiguity."""
    candidates = [f"{EX}EmailMessage", f"{EX}EmailCampaign", f"{EX}Party"]
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:Party a rdfs:Class .")),
        generator=_Gen("SELECT ?x WHERE { ?x ?p ?o }"),
        run_execution=lambda sparql, **_kw: {"rows": []},
    )
    ctx = tier2_vkg_workflow(question="show email", namespace="ns", deps=deps)
    assert ctx.needs_clarification is not None
    assert ctx.clarification_source == "phase2"


def test_property_collision_clarifies():
    # ex:amount has domain on two unrelated classes in the slice → Phase 3b
    # clarification, no execution.
    slice_ttl = _slice_ttl(
        "ex:Order a rdfs:Class . ex:Payment a rdfs:Class . "
        "ex:amount rdfs:domain ex:Order . ex:amount rdfs:domain ex:Payment ."
    )
    deps = PhaseDeps(
        router=_Router([f"{EX}Order", f"{EX}Payment"]),
        builder=_Builder(slice_ttl),
        generator=_Gen("SELECT ?x WHERE { ?x ?p ?o }"),
        run_execution=lambda sparql, **_kw: {"rows": []},
    )
    ctx = tier2_vkg_workflow(question="total amount", namespace="ns", deps=deps)
    assert ctx.needs_clarification is not None
    assert ctx.clarification_source == "phase3b"


def test_phase2_sibling_classes_collapse_to_base_not_clarification():
    """Regression (nb6 gt-row-07): on the 40-table VKG ontology, the head noun
    'hold'/'holding' substring-matches a BASE class (Holding) AND its derived
    siblings (HoldingLoan, HoldingSubaccount, HoldingPayout). That is not a real
    entity ambiguity — Phase 2 must resolve to the base Holding class instead of
    asking 'Which interpretation of hold do you mean?'."""
    base = f"{EX}Holding"
    candidates = [
        base,
        f"{EX}HoldingLoan",
        f"{EX}HoldingSubaccount",
        f"{EX}HoldingPayout",
        f"{EX}Party",
    ]
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:Holding a rdfs:Class .")),
        generator=_Gen("SELECT ?x WHERE { ?x a ex:Holding }"),
        run_execution=lambda sparql, **_kw: {"columns": ["x"], "rows": [["1"]],
                                      "answer": "1 row"},
    )
    ctx = tier2_vkg_workflow(question="total market value of holdings",
                             namespace="ns", deps=deps)
    assert ctx.needs_clarification is None, ctx.needs_clarification
    resolved = {b.get("iri") for b in ctx.disambiguation.values()
                if isinstance(b, dict)}
    assert base in resolved, ctx.disambiguation


def test_phase2_count_question_no_clarify_when_base_class_absent():
    """Regression (2026-06-30, VKG 0.54→0.44): 'How many parties are there?' on the
    rebuilt layer spuriously asked 'Which interpretation of parties?' and emitted no
    SQL. Root cause: Phase-1 KNN surfaced only the sibling classes (PartyBanking,
    PartyLicense) and NOT the base Party class, so the plural head noun fell to a
    Tier-2 substring tie with no common base IN the candidate set — the existing
    base-class-collapse could not fire. The shared-stem-sibling collapse resolves it
    (plural term → subtype is a non-choice), so a count question answers instead of
    clarifying. Singular ties (e.g. 'show email' → EmailMessage/EmailCampaign) still
    clarify — see test_phase2_two_distinct_classes_still_clarifies."""
    candidates = [
        f"{EX}PartyBanking",
        f"{EX}PartyLicense",
        f"{EX}PartyBanking/party_id",
        f"{EX}PartyLicense/party_id",
    ]  # NOTE: base ex:Party deliberately ABSENT from Phase-1 candidates.
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:PartyBanking a rdfs:Class .")),
        generator=_Gen("SELECT (COUNT(*) AS ?n) WHERE { ?x a ex:PartyBanking }"),
        run_execution=lambda sparql, **_kw: {"columns": ["n"], "rows": [["15"]],
                                             "answer": "15"},
    )
    ctx = tier2_vkg_workflow(question="How many parties are there?",
                             namespace="ns", deps=deps)
    assert ctx.needs_clarification is None, ctx.needs_clarification
    resolved = {b.get("iri") for b in ctx.disambiguation.values()
                if isinstance(b, dict)}
    # 'parties' collapsed to a sibling class (the top-ranked one), not clarified.
    assert resolved & set(candidates), ctx.disambiguation


def test_phase2_shared_head_classes_collapse_not_clarification():
    """Regression (nb5 gt-row-07): the head noun 'product' matches two SUFFIX-shared
    sibling classes (CoverageProduct, PolicyProduct) that have the SAME head
    ('Product') but NO common prefix — so the base-class (prefix) collapse misses
    them and Phase 2 spuriously asked 'which interpretation: product?'. Picking
    Coverage- vs Policy-Product is a join-path detail the generator + grounding gate
    own (the flat-KB metadata agent resolves it without asking). Phase 2 must
    resolve to the top-ranked product class, not clarify."""
    candidates = [
        f"{EX}CoverageProduct",
        f"{EX}PolicyProduct",
        f"{EX}Party",
    ]
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:CoverageProduct a rdfs:Class .")),
        generator=_Gen("SELECT ?x WHERE { ?x a ex:CoverageProduct }"),
        run_execution=lambda sparql, **_kw: {"columns": ["x"], "rows": [["1"]],
                                      "answer": "1 row"},
    )
    ctx = tier2_vkg_workflow(question="top 10 parties by holding value including "
                             "the investment product names",
                             namespace="ns", deps=deps)
    assert ctx.needs_clarification is None, ctx.needs_clarification
    resolved = {b.get("iri") for b in ctx.disambiguation.values()
                if isinstance(b, dict)}
    # Resolved to the top-RANKED (first-listed) product class, not clarified.
    assert f"{EX}CoverageProduct" in resolved, ctx.disambiguation


def test_phase2_redundant_stem_fragment_does_not_clarify():
    """Regression (nb5 gt-row-07, the 'hold' half): the question
    'top parties by total HOLDING market value … they HOLD' yields BOTH 'holding'
    (resolves CLEAR to the Holding class) AND the bare verb 'hold'. Left alone,
    'hold' loosely substring-matches the whole Holding family PLUS unrelated
    classes like Policyholder (which 'holding' never matches) — so neither the
    prefix nor the shared-head collapse fires and it spuriously clarifies 'which
    interpretation of hold?'. Phase 2 must skip the redundant stem fragment because
    the longer 'holding' already pinned the real entity."""
    candidates = [
        f"{EX}Holding",
        f"{EX}HoldingSubaccount",
        f"{EX}HoldingPayout",
        f"{EX}Policyholder",  # substring-matches 'hold' but NOT 'holding'
        f"{EX}Party",
    ]
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:Holding a rdfs:Class .")),
        generator=_Gen("SELECT ?x WHERE { ?x a ex:Holding }"),
        run_execution=lambda sparql, **_kw: {"columns": ["x"], "rows": [["1"]],
                                      "answer": "1 row"},
    )
    ctx = tier2_vkg_workflow(
        question="top 10 parties by total holding market value they hold",
        namespace="ns", deps=deps)
    assert ctx.needs_clarification is None, ctx.needs_clarification
    # 'holding' resolved CLEAR to Holding; 'hold' was skipped (not clarified).
    resolved = {b.get("iri") for b in ctx.disambiguation.values()
                if isinstance(b, dict)}
    assert f"{EX}Holding" in resolved, ctx.disambiguation


def test_phase2_property_only_term_does_not_clarify():
    """Regression (nb6 gt-row-04/06): a generic ATTRIBUTE word like 'name'
    matches a `name` property on MANY classes (party.name, product.name, …) and
    NO class. That is not a real entity ambiguity — 'Which interpretation of
    name?' is un-actionable (every option is a *_name attribute). Phase 2 must
    defer to Phase-1 ranking and proceed, NOT clarify. The user named the real
    entity elsewhere ('coverage products by name'); the descriptive term must
    not hijack a clarification. Holds even though 'name' EXACTLY names a property."""
    cp = f"{EX}CoverageProduct"
    candidates = [
        cp,
        f"{EX}CoverageProduct/name",   # exact 'name' property on the entity
        f"{EX}Party/name",             # 'name' also on Party
        f"{EX}Party",
    ]
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:CoverageProduct a rdfs:Class .")),
        generator=_Gen("SELECT ?n WHERE { ?x a ex:CoverageProduct ; ex:name ?n }"),
        run_execution=lambda sparql, **_kw: {"columns": ["n"], "rows": [["P1"]],
                                      "answer": "1 row"},
    )
    ctx = tier2_vkg_workflow(question="list the top 10 coverage products by name",
                             namespace="ns", deps=deps)
    assert ctx.needs_clarification is None, ctx.needs_clarification
    # 'name' resolved to the highest-ranked matching IRI (fuzzy_top_rank), and the
    # real entity (CoverageProduct) resolved to its class.
    resolved = {b.get("iri") for b in ctx.disambiguation.values()
                if isinstance(b, dict)}
    assert cp in resolved, ctx.disambiguation


def test_phase2_generic_label_attr_never_clarifies_even_if_class_matches():
    """Regression (nb6 gt-row-04): on a large layer the descriptive term 'name'
    can substring-match a CLASS local name (e.g. a 'NamedEntity' class) in
    addition to many *_name properties, which would skip the property-only guard
    and clarify 'Which interpretation of name?'. A universal label attribute is
    never an entity choice — Phase 2 must bind it to the top-ranked match and
    proceed regardless of class matches. The real head entity is named elsewhere."""
    party = f"{EX}Party"
    candidates = [
        party,
        f"{EX}NameComponent",        # a CLASS whose local name contains 'name'
        f"{EX}Party/name",           # 'name' property on Party
        f"{EX}CoverageProduct",
        f"{EX}CoverageProduct/name",
    ]
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:Party a rdfs:Class .")),
        generator=_Gen("SELECT ?n WHERE { ?x a ex:Party ; ex:name ?n }"),
        run_execution=lambda sparql, **_kw: {"columns": ["n"], "rows": [["X"]],
                                      "answer": "1 row"},
    )
    ctx = tier2_vkg_workflow(
        question="show the policyholder's name and the coverage product",
        namespace="ns", deps=deps)
    assert ctx.needs_clarification is None, ctx.needs_clarification
    # 'name' bound (not escalated); a real entity still resolved.
    binding = ctx.disambiguation.get("name")
    assert binding and binding.get("status") == "CLEAR", ctx.disambiguation


def test_phase2_bare_entityless_question_clarifies_first():
    """Fix 4 (nb5 mt-parties / mt-stable): a bare 'How many are there?' strips to
    ZERO significant terms, so the agent must ASK which entity to count — offering
    the candidate CLASSES as options — rather than guessing a class."""
    candidates = [f"{EX}Party", f"{EX}Coverage", f"{EX}Holding"]
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:Party a rdfs:Class .")),
        generator=_Gen("SELECT (COUNT(?x) AS ?n) WHERE { ?x a ex:Party }"),
        run_execution=lambda sparql, **_kw: {"rows": []},
    )
    ctx = tier2_vkg_workflow(question="How many are there?", namespace="ns",
                             deps=deps)
    assert ctx.needs_clarification is not None
    assert ctx.clarification_source == "phase2_no_entity"
    # The candidate classes are offered as options (so the user can pick one).
    # _local_name lower-cases the label.
    labels = {o["label"] for o in ctx.needs_clarification["options"]}
    assert {"party", "coverage", "holding"} <= labels
    assert "count or list" in ctx.needs_clarification["clarification_question"].lower()


def test_phase2_entityless_question_with_no_candidates_degrades_not_clarifies():
    """A bare quantity question with NO candidates at all routes to phase1_empty
    (degraded) BEFORE Phase 2 — the no-entity guard never runs, so there is no
    spurious clarification."""
    deps = PhaseDeps(
        router=_Router([]),  # Phase 1 finds nothing → phase1_empty → degraded
        builder=_Builder(_slice_ttl("ex:Party a rdfs:Class .")),
        generator=_Gen("SELECT ?x WHERE { ?x ?p ?o }"),
        run_execution=lambda sparql, **_kw: {"rows": []},
    )
    ctx = tier2_vkg_workflow(question="how many are there?", namespace="ns",
                             deps=deps)
    assert ctx.degraded == "phase1_empty"
    assert ctx.clarification_source != "phase2_no_entity"


def test_phase2_no_entity_guard_skips_garbage_non_quantity_question():
    """A term-less question that is NOT a quantity/existence ask (e.g. a stray
    token) must NOT force-clarify — it falls through to the normal path so the
    grounding/degrade machinery handles it (guards the grounding-ceiling probe)."""
    candidates = [f"{EX}Party"]
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:Party a rdfs:Class .")),
        generator=_Gen("SELECT ?x WHERE { ?x a ex:Party }"),
        run_execution=lambda sparql, **_kw: {"columns": ["x"], "rows": [["1"]],
                                      "answer": "ok"},
    )
    # "the" strips to zero terms but is not a quantity ask → guard must NOT fire.
    ctx = tier2_vkg_workflow(question="the", namespace="ns", deps=deps)
    assert ctx.clarification_source != "phase2_no_entity"


def test_phase2_question_naming_an_entity_skips_no_entity_guard():
    """A question that DOES name an entity ('how many parties?') must resolve
    normally — the no-entity clarify-first must not fire when a term is present."""
    candidates = [f"{EX}Party"]
    deps = PhaseDeps(
        router=_Router(candidates),
        builder=_Builder(_slice_ttl("ex:Party a rdfs:Class .")),
        generator=_Gen("SELECT (COUNT(?x) AS ?n) WHERE { ?x a ex:Party }"),
        run_execution=lambda sparql, **_kw: {"columns": ["n"], "rows": [["15"]],
                                      "answer": "15 parties"},
    )
    ctx = tier2_vkg_workflow(question="how many parties are there?",
                             namespace="ns", deps=deps)
    assert ctx.needs_clarification is None, ctx.needs_clarification
    assert ctx.clarification_source != "phase2_no_entity"


# ── P2-2: fabricated-namespace guard ─────────────────────────────────────────
def test_namespace_helpers_derive_base_and_flag_foreign():
    from agents.ontology_query_agent.tier2.workflow import (
        _slice_ontology_base, _is_foreign_namespace, _namespace_of,
    )
    # Real slice shape: full-IRI subjects, owl:Class object (CURIE or full IRI).
    slice_ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "<http://base/ontology/L/Holding> a owl:Class .\n"
        "<http://base/ontology/L/Party> a owl:Class .\n"
        "<http://base/ontology/L/Holding/market_value> a owl:DatatypeProperty .\n"
    )
    base = _slice_ontology_base(slice_ttl)
    assert base == "http://base/ontology/L/"
    assert _namespace_of("http://base/ontology/L/Holding/market_value") == \
        "http://base/ontology/L/Holding/"
    # Fabricated foreign namespace (the gt-04 example.org hallucination).
    assert _is_foreign_namespace(
        "https://example.org/ontology/HoldingPayout/payout_frequency", base) is True
    # Real same-base IRI is NOT foreign.
    assert _is_foreign_namespace("http://base/ontology/L/Holding/holding_id", base) is False
    # W3C / vkg system namespaces are NOT foreign.
    assert _is_foreign_namespace("http://www.w3.org/2000/01/rdf-schema#label", base) is False
    assert _is_foreign_namespace(
        "https://semantic-layer.aws/virtual-kg/mapsToColumn", base) is False
    # No derivable base → nothing is foreign (conservative no-op).
    assert _is_foreign_namespace("https://example.org/x/y", "") is False


def test_phase3_overrides_when_judge_names_only_fabricated_namespace():
    # gt-04 shape: the judge degrades naming ONLY fabricated-namespace IRIs
    # (example.org) whose local names aren't in the slice. The fabricated-namespace
    # guard must recognise them as unfetchable hallucinations and proceed to Phase 4,
    # not loop to a phase3_max_rounds degrade.
    from agents.ontology_query_agent.tier2.workflow import tier2_vkg_workflow
    slice_ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "<http://base/ontology/L/AnnuityDetail> a owl:Class .\n"
        "<http://base/ontology/L/AnnuityDetail/holding_id> a owl:DatatypeProperty .\n"
    )
    runs = {"gen": 0, "exec": 0}

    class _OnlyFabricated(_Builder):
        def is_sufficient(self, *, slice_text, question):
            return False, [
                "https://example.org/ontology/HoldingPayout/payout_frequency",
                "https://example.org/ontology/HoldingPayout/payout_amount",
            ]

    class _CountingGen(_Gen):
        def generate(self, *, slice_text, question, grounding_feedback=""):
            runs["gen"] += 1
            return super().generate(slice_text=slice_text, question=question,
                                    grounding_feedback=grounding_feedback)

    def _exec(sparql, **_kw):
        runs["exec"] += 1
        return {"columns": ["x"], "rows": [["1"]], "answer": "1", "n_quads": ["q"]}

    deps = PhaseDeps(
        router=_Router(["http://base/ontology/L/AnnuityDetail"]),
        builder=_OnlyFabricated(slice_ttl),
        generator=_CountingGen("SELECT ?x WHERE { ?x a <http://base/ontology/L/AnnuityDetail> }"),
        run_execution=_exec,
    )
    ctx = tier2_vkg_workflow(question="payout schedules", namespace="ns", deps=deps)
    assert ctx.degraded != "phase3_max_rounds", \
        "all-fabricated-namespace missing must override, not degrade"
    assert runs["gen"] == 1 and runs["exec"] == 1


def test_phase3_does_not_override_genuinely_absent_same_namespace():
    # A genuinely-absent SAME-namespace IRI must still degrade (guard must not
    # over-drop). The judge names a real-looking but absent same-base concept.
    from agents.ontology_query_agent.tier2.workflow import tier2_vkg_workflow
    slice_ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "<http://base/ontology/L/Party> a owl:Class .\n"
    )

    class _RealAbsent(_Builder):
        def is_sufficient(self, *, slice_text, question):
            # same base, but this class is NOT in the slice and never fetchable here
            return False, ["http://base/ontology/L/NonexistentBridgeClass"]

    deps = PhaseDeps(
        router=_Router(["http://base/ontology/L/Party"]),
        builder=_RealAbsent(slice_ttl),
        generator=_Gen("SELECT ?x WHERE { ?x a <http://base/ontology/L/Party> }"),
        run_execution=lambda sparql, **_kw: {"rows": [], "columns": [], "answer": "0", "n_quads": []},
    )
    ctx = tier2_vkg_workflow(question="x related to y", namespace="ns", deps=deps)
    # genuinely-absent same-namespace IRI → NOT overridden by the fabricated guard
    assert ctx.degraded == "phase3_max_rounds", \
        "a real same-namespace absent IRI must still degrade (no over-drop)"
