"""Unit tests for the Tier 2 Strands graph workflow (tier2/workflow.py).

These drive the REAL Strands Graph engine (conftest loads the genuine
strands.multiagent submodules) with stubbed phase dependencies, so they
exercise the actual node ordering, conditional edges, and the grounding
loop-back back-edge.
"""
import json

from agents.metadata_query_agent.tier2.workflow import (
    PhaseDeps,
    tier2_rag_workflow,
)


def _slice(columns, tables):
    return json.dumps({
        "tables": tables,
        "columns": [{"table_id": t, "name": c} for t, c in columns],
        "joins": [],
    })


class _Router:
    def __init__(self, candidates, structured=None):
        self._candidates = candidates
        self.last_structured = structured or {
            "candidates": [{"table_id": t, "score": 0.9} for t in candidates],
            "chunks_by_table": {t: f"# {t}" for t in candidates},
        }

    def find_candidates(self, *, question, namespace):
        return list(self._candidates)


class _Builder:
    def __init__(self, slice_text, sufficient=True, on_expand=None):
        self._slice = slice_text
        self._sufficient = sufficient
        self._on_expand = on_expand

    def build(self, *, candidates, namespace):
        return self._slice

    def is_sufficient(self, *, slice_text, question):
        return self._sufficient, None

    def expand(self, *, slice_text, missing):
        if self._on_expand:
            return self._on_expand(slice_text, missing)
        return slice_text


class _Gen:
    """SQL generator stub.

    ``sql`` may be a string (always returned) or a callable
    ``(grounding_feedback) -> str`` so a test can model regeneration: return a
    hallucinated query on the first call (no feedback) and a clean one once the
    grounding gate feeds back the missing identifiers.
    """

    def __init__(self, sql):
        self._sql = sql

    def generate(self, *, slice_text, question, grounding_feedback=""):
        if callable(self._sql):
            return self._sql(grounding_feedback)
        return self._sql


def _events_collector():
    events = []

    def sink(phase, action, payload):
        events.append((phase, action, payload.get("step")))

    return events, sink


def test_happy_path_runs_all_phases_in_order():
    slice_text = _slice([("db.customers", "id")], ["db.customers"])
    deps = PhaseDeps(
        router=_Router(["db.customers"]),
        builder=_Builder(slice_text),
        generator=_Gen("SELECT id FROM customers"),
        run_execution=lambda sql, db, cat, **_kw: {"columns": ["id"], "rows": [["1"]],
                                            "answer": "1 row"},
    )
    events, sink = _events_collector()
    ctx = tier2_rag_workflow(question="customers", namespace="ns", kb_id="kb",
                             deps=deps, phase_sink=sink)
    assert ctx.degraded is None
    assert ctx.needs_clarification is None
    assert ctx.execution_result["rows"] == [["1"]]
    # phases 1,2,3,3b,4,5 each fire a start
    starts = [(p, step) for (p, a, step) in events if a == "phase_start"]
    assert (1, None) in starts and (2, None) in starts
    assert (3, None) in starts and (3, "3b") in starts
    assert (4, None) in starts and (5, None) in starts


def test_empty_candidates_routes_to_degraded():
    deps = PhaseDeps(
        router=_Router([], structured={"candidates": [], "chunks_by_table": {}}),
        builder=_Builder("{}"),
        generator=_Gen("SELECT 1"),
        run_execution=lambda *a, **_kw: {"rows": []},
    )
    ctx = tier2_rag_workflow(question="x", namespace="ns", kb_id="kb", deps=deps)
    assert ctx.degraded == "phase1_empty"
    assert ctx.execution_result == {}  # never executed


def test_grounding_loopback_regenerates_sql_then_succeeds():
    # Round 1 SQL hallucinates 'ssn' (not in slice) → grounding gate loops back
    # to Phase 4 with feedback → regenerated SQL uses only slice columns →
    # grounds and executes. The slice is NOT widened (the column doesn't exist).
    initial = _slice([("db.customers", "id")], ["db.customers"])
    runs = {"n": 0}

    def gen(feedback):
        # First call (no feedback) hallucinates; once fed back, regenerate clean.
        return ("SELECT id FROM customers" if feedback
                else "SELECT id, ssn FROM customers")

    def run_exec(sql, db, cat, **_kw):
        runs["n"] += 1
        return {"columns": ["id"], "rows": [["1"]]}

    deps = PhaseDeps(
        router=_Router(["db.customers"]),
        builder=_Builder(initial),
        generator=_Gen(gen),
        run_execution=run_exec,
    )
    ctx = tier2_rag_workflow(question="customers", namespace="ns",
                             kb_id="kb", deps=deps)
    assert ctx.grounding_rounds == 1
    assert ctx.degraded is None
    assert ctx.grounding_feedback  # feedback was captured
    assert runs["n"] == 1  # executed exactly once, after regeneration grounded


def test_grounding_ceiling_degrades_without_executing():
    # SQL keeps hallucinating 'ssn' every round → grounding never resolves →
    # ceiling hit → degrade without ever executing.
    initial = _slice([("db.customers", "id")], ["db.customers"])
    runs = {"n": 0}

    def run_exec(sql, db, cat, **_kw):
        runs["n"] += 1
        return {"rows": []}

    deps = PhaseDeps(
        router=_Router(["db.customers"]),
        builder=_Builder(initial),
        generator=_Gen("SELECT id, ssn FROM customers"),
        run_execution=run_exec,
    )
    ctx = tier2_rag_workflow(question="x", namespace="ns", kb_id="kb", deps=deps)
    assert ctx.degraded == "grounding_unresolved"
    assert runs["n"] == 0  # never executed un-grounded SQL


def test_slice_disambiguation_clarifies_on_collision():
    # 'amount' on two unconnected slice tables → Phase 3b clarification.
    slice_text = _slice([("db.orders", "amount"), ("db.payments", "amount")],
                        ["db.orders", "db.payments"])
    deps = PhaseDeps(
        router=_Router(["db.orders", "db.payments"]),
        builder=_Builder(slice_text),
        generator=_Gen("SELECT amount FROM orders"),
        run_execution=lambda *a, **_kw: {"rows": []},
    )
    ctx = tier2_rag_workflow(question="total amount", namespace="ns", kb_id="kb",
                             deps=deps)
    assert ctx.needs_clarification is not None
    assert ctx.clarification_source == "phase3b"


def test_clarification_resolution_prunes_phase2_no_reclarify_loop():
    # Regression for the RAG Phase 2 clarification loop (docs/plans/disambiguation.md):
    # Phase 1 returns two same-name rival tables (party_license, party_banking);
    # the user already chose party_license on a prior turn. Phase 1 prunes the
    # rival, but Phase 2 historically disambiguated against the RAW router
    # payload (which still held both) → re-fired the identical clarification.
    # With the fix, Phase 2 honors the pruned ctx.candidates → term resolves
    # CLEAR and the graph proceeds to execute.
    from agents.shared.clarification import ClarificationResolution

    rivals = ["normalized.party_license", "normalized.party_banking"]
    structured = {
        "candidates": [{"table_id": t, "score": 0.9} for t in rivals],
        "chunks_by_table": {t: f"# {t}" for t in rivals},
    }
    slice_text = _slice([("normalized.party_license", "party_type")],
                        ["normalized.party_license"])
    deps = PhaseDeps(
        router=_Router(rivals, structured=structured),
        builder=_Builder(slice_text),
        generator=_Gen("SELECT party_type FROM party_license"),
        run_execution=lambda sql, db, cat, **_kw: {
            "columns": ["party_type"], "rows": [["individual"]], "answer": "1 row"},
    )
    resolution = ClarificationResolution(
        original_question="List the top 5 most common party types",
        chosen_ids=["party_license"],
        rival_ids=["party_license", "party_banking"],
    )
    ctx = tier2_rag_workflow(
        question="List the top 5 most common party types", namespace="ns",
        kb_id="kb", deps=deps, clarification_resolution=resolution,
    )
    # The rival was pruned in Phase 1, so Phase 2 must NOT re-clarify.
    assert ctx.needs_clarification is None, "Phase 2 re-fired the clarification (loop)"
    assert ctx.candidates == ["normalized.party_license"], "rival not pruned"
    # And the graph proceeded all the way to execution.
    assert ctx.degraded is None
    assert ctx.execution_result.get("rows") == [["individual"]]


def test_parties_question_no_low_confidence_loop():
    # Regression for the "How many parties are there?" loop (session 4c8a50c7).
    # `party` scores below the 0.4 floor and is the only candidate that matters,
    # but "parties" -> "party" is an exact name match, so Phase 2 must proceed
    # CLEAR (no clarification) on the FIRST turn — no resolution needed.
    structured = {
        "candidates": [
            {"table_id": "normalized.party", "score": 0.34},
            {"table_id": "normalized.govt_id_info", "score": 0.12},
            {"table_id": "normalized.address", "score": 0.11},
        ],
        "chunks_by_table": {"normalized.party": "# party"},
    }
    slice_text = _slice([("normalized.party", "party_id")], ["normalized.party"])
    deps = PhaseDeps(
        router=_Router(["normalized.party", "normalized.govt_id_info",
                        "normalized.address"], structured=structured),
        builder=_Builder(slice_text),
        generator=_Gen("SELECT COUNT(*) FROM party"),
        run_execution=lambda sql, db, cat, **_kw: {
            "columns": ["_col0"], "rows": [["42"]], "answer": "42"},
    )
    ctx = tier2_rag_workflow(question="How many parties are there?",
                             namespace="ns", kb_id="kb", deps=deps)
    assert ctx.needs_clarification is None, "low-confidence clarification re-fired"
    assert ctx.degraded is None
    assert ctx.execution_result.get("rows") == [["42"]]


def test_confirmed_pick_breaks_low_confidence_loop():
    # Even when NO term lexically names the table (so the exact-match suppression
    # can't help) and the score is below the floor, a turn that ANSWERS a prior
    # clarification must proceed — the pick is a confident binding. Without this,
    # the low-confidence clarification is unresolvable (picking can't raise the
    # cosine score) and loops forever.
    from agents.shared.clarification import ClarificationResolution

    structured = {
        "candidates": [
            {"table_id": "normalized.party", "score": 0.20},
            {"table_id": "normalized.relation", "score": 0.18},
        ],
        "chunks_by_table": {"normalized.party": "# party"},
    }
    slice_text = _slice([("normalized.party", "party_id")], ["normalized.party"])
    deps = PhaseDeps(
        router=_Router(["normalized.party", "normalized.relation"],
                       structured=structured),
        builder=_Builder(slice_text),
        generator=_Gen("SELECT COUNT(*) FROM party"),
        run_execution=lambda sql, db, cat, **_kw: {
            "columns": ["_col0"], "rows": [["42"]], "answer": "42"},
    )
    # The user picked "party"; "relation" is the pruned rival.
    resolution = ClarificationResolution(
        original_question="how many of them are there",
        chosen_ids=["party"],
        rival_ids=["relation"],
    )
    ctx = tier2_rag_workflow(question="how many of them are there", namespace="ns",
                             kb_id="kb", deps=deps,
                             clarification_resolution=resolution)
    assert ctx.needs_clarification is None, "confirmed pick still re-clarified (loop)"
    assert ctx.degraded is None
    assert ctx.execution_result.get("rows") == [["42"]]


def test_low_confidence_reask_reuses_prior_options():
    # Fix 3: a low-confidence clarification (no specific ambiguous term) that
    # re-fires on a re-ask must reuse the PRIOR turn's options, not a fresh
    # non-deterministic top-5. Here Phase 1 returns a DIFFERENT candidate set
    # than the prior turn; the emitted clarification must still carry the prior
    # options so the user sees a stable list.
    structured = {
        "candidates": [
            {"table_id": "normalized.distribution_level", "score": 0.12},
            {"table_id": "normalized.carrier_appointment", "score": 0.11},
        ],
        "chunks_by_table": {},
    }
    deps = PhaseDeps(
        router=_Router(["normalized.distribution_level",
                        "normalized.carrier_appointment"], structured=structured),
        builder=_Builder("{}"),
        generator=_Gen("SELECT 1"),
        run_execution=lambda *a, **_kw: {"rows": []},
    )
    prior_opts = [
        {"id": "party", "label": "party (database: normalized)"},
        {"id": "govt_id_info", "label": "govt_id_info (database: normalized)"},
    ]
    ctx = tier2_rag_workflow(
        question="show me the important stuff", namespace="ns", kb_id="kb",
        deps=deps, prior_clarification_options=prior_opts,
        prior_clarification_terms=["important stuff"],
    )
    assert ctx.needs_clarification is not None
    opt_ids = [o["id"] for o in ctx.needs_clarification["options"]]
    assert opt_ids == ["party", "govt_id_info"], "did not reuse prior options"
    # The churny fresh candidates must NOT have leaked into the options.
    assert "distribution_level" not in opt_ids


def test_phase3_insufficient_slice_short_circuits_to_degraded():
    # When the slice judge never reaches sufficiency within MAX_PHASE3_ROUNDS,
    # the graph must degrade (phase3_max_rounds) and NOT generate/execute SQL
    # against the rejected slice — that previously produced a misleading 0-row
    # answer.
    runs = {"exec": 0}
    slice_text = _slice([("db.customers", "id")], ["db.customers"])
    deps = PhaseDeps(
        router=_Router(["db.customers"]),
        builder=_Builder(slice_text, sufficient=False),
        generator=_Gen("SELECT id FROM customers"),
        run_execution=lambda *a, **_kw: runs.__setitem__("exec", runs["exec"] + 1) or {"rows": []},
    )
    payloads = []
    ctx = tier2_rag_workflow(question="customers", namespace="ns", kb_id="kb",
                             deps=deps,
                             phase_sink=lambda ph, a, pl: payloads.append((ph, a, pl)))
    assert ctx.degraded == "phase3_max_rounds"
    assert runs["exec"] == 0, "Phase 5 must not execute on an insufficient slice"
    assert ctx.execution_result == {}
    # phase_result must carry per-round judge diagnostics for a degrade.
    p3 = [pl for (ph, a, pl) in payloads
          if ph == 3 and a == "phase_result" and pl.get("step") is None]
    assert p3, "expected a Phase 3 phase_result"
    detail = p3[0].get("judgeRoundsDetail")
    assert isinstance(detail, list) and len(detail) >= 1
    assert all(r["sufficient"] is False for r in detail)
    assert "sliceTokens" in detail[0] and "missing" in detail[0]


def test_b1_role_enumeration_does_not_fast_fail_gt00_at_phase3b():
    # De-layered guard: the role vocabulary is parsed from the slice's own column
    # enumeration. The B1-enriched `life_participant` slice declares BOTH
    # Owner/Policyholder and Insured as policy party-roles, so gt-00 ("insured ...
    # also the policyholder") references only roles the slice CAN represent →
    # Phase 3b must NOT fast-fail; Phase 4 runs to build the self-join. This is the
    # core sequencing guarantee of the design (§2 note / §5): once B1 lands,
    # detect_unsupported_relationship stops firing for gt-00.
    runs = {"gen": 0}
    slice_obj = {
        "tables": ["normalized.life_participant"],
        "columns": [
            {"table_id": "normalized.life_participant", "name": "participant_role",
             "description": "Role of the party on the policy. Role values "
                            "include: Owner (synonyms: Policyholder), Insured, "
                            "Beneficiary. Each value is a distinct policy "
                            "party-role."},
            {"table_id": "normalized.life_participant", "name": "holding_id",
             "description": "FK to holding. Self-join key (pair with party_id)."},
            {"table_id": "normalized.life_participant", "name": "party_id",
             "description": "FK to party."},
        ],
        "joins": [],
    }

    class _GenCounting:
        def generate(self, *, slice_text, question, grounding_feedback=""):
            runs["gen"] += 1
            return "SELECT 1"

    deps = PhaseDeps(
        router=_Router(["normalized.life_participant"]),
        builder=_Builder(json.dumps(slice_obj)),
        generator=_GenCounting(),
        run_execution=lambda *a, **_kw: {"columns": [], "rows": [], "answer": "0"},
    )
    ctx = tier2_rag_workflow(
        question="Show me policies where the insured party is also the policyholder.",
        namespace="ns", kb_id="kb", deps=deps)
    assert ctx.degraded != "relationship_unsupported"
    assert runs["gen"] == 1, "Phase 4 must run — gt-00 is answerable with B1"


def test_no_role_enumeration_does_not_fast_fail_at_phase3b():
    # When the slice declares NO policy party-role enumeration (the real curated
    # `relation` schema carries only interpersonal Primary/Secondary roles), the
    # de-layered guard is a no-op: absent supporting metadata it must NOT invent a
    # domain-specific fast-fail. Phase 4 runs and the grounding gate is the
    # backstop (design §4c).
    runs = {"gen": 0}
    slice_obj = {
        "tables": ["normalized.relation", "normalized.coverage"],
        "columns": [
            {"table_id": "normalized.relation", "name": "relationship_role",
             "description": "Values: Primary, Secondary."},
            {"table_id": "normalized.coverage", "name": "party_id",
             "description": "Insured party on this coverage."},
        ],
        "joins": [],
    }

    class _GenCounting:
        def generate(self, *, slice_text, question, grounding_feedback=""):
            runs["gen"] += 1
            return "SELECT 1"

    deps = PhaseDeps(
        router=_Router(["normalized.relation", "normalized.coverage"]),
        builder=_Builder(json.dumps(slice_obj)),
        generator=_GenCounting(),
        run_execution=lambda *a, **_kw: {"columns": [], "rows": [], "answer": "0"},
    )
    ctx = tier2_rag_workflow(
        question="Show me policies where the insured party is also the policyholder.",
        namespace="ns", kb_id="kb", deps=deps)
    assert ctx.degraded != "relationship_unsupported"
    assert runs["gen"] == 1, "Phase 4 must run when no role enumeration is declared"


class _JudgeBuilder:
    """Builder stub whose judge returns a fixed (sufficient, missing) verdict.

    ``on_expand`` lets a test model expand() either widening the slice or being a
    no-op (returning the same text), to exercise the early-exit path.
    """

    def __init__(self, slice_text, *, sufficient, missing, on_expand=None):
        self._slice = slice_text
        self._sufficient = sufficient
        self._missing = missing
        self._on_expand = on_expand
        self.judge_usage = {}

    def build(self, *, candidates, namespace):
        return self._slice

    def is_sufficient(self, *, slice_text, question):
        return self._sufficient, list(self._missing)

    def expand(self, *, slice_text, missing):
        if self._on_expand:
            return self._on_expand(slice_text, missing)
        return slice_text  # no-op by default


def test_judge_false_negative_override_proceeds_when_missing_present():
    # Regression for the 'curated.party' over-rejection (session 0bdcba9a): the
    # judge fabricates a layer prefix the user typed ('curated.party') while the
    # slice carries 'normalized.party'. The deterministic self-contradiction
    # override must recognise that the missing TABLE is present (by name) and
    # proceed to SQL generation + execution instead of degrading.
    slice_text = _slice([("normalized.party", "party_id")], ["normalized.party"])
    deps = PhaseDeps(
        router=_Router(["normalized.party"]),
        builder=_JudgeBuilder(slice_text, sufficient=False,
                              missing=["curated.party"]),
        generator=_Gen("SELECT COUNT(*) FROM party"),
        run_execution=lambda sql, db, cat, **_kw: {
            "columns": ["_col0"], "rows": [["42"]], "answer": "42"},
    )
    payloads = []
    ctx = tier2_rag_workflow(question="How many parties exist in the curated layer?",
                             namespace="ns", kb_id="kb", deps=deps,
                             phase_sink=lambda ph, a, pl: payloads.append((ph, a, pl)))
    assert ctx.degraded is None, "override should have proceeded past the judge"
    assert ctx.execution_result.get("rows") == [["42"]]
    # The override must be recorded in the per-round trace.
    p3 = [pl for (ph, a, pl) in payloads
          if ph == 3 and a == "phase_result" and pl.get("step") is None]
    assert p3 and p3[0]["judgeRoundsDetail"][-1].get("overrodeJudgeFalseNegative")


def test_judge_override_does_not_fire_on_genuinely_absent_table():
    # The override is conservative: when the missing table is truly absent from
    # the slice, it must NOT fire — the loop degrades as before.
    slice_text = _slice([("normalized.party", "party_id")], ["normalized.party"])
    deps = PhaseDeps(
        router=_Router(["normalized.party"]),
        builder=_JudgeBuilder(slice_text, sufficient=False,
                              missing=["normalized.payout"]),
        generator=_Gen("SELECT 1"),
        run_execution=lambda *a, **_kw: {"rows": []},
    )
    ctx = tier2_rag_workflow(question="payout schedule", namespace="ns",
                             kb_id="kb", deps=deps)
    assert ctx.degraded == "phase3_max_rounds"


def test_judge_override_matches_missing_column_by_name():
    # A db.table.column missing entry whose column IS in the slice is also a
    # false negative → override proceeds.
    slice_text = _slice([("normalized.party", "party_type")], ["normalized.party"])
    deps = PhaseDeps(
        router=_Router(["normalized.party"]),
        builder=_JudgeBuilder(slice_text, sufficient=False,
                              missing=["curated.party.party_type"]),
        generator=_Gen("SELECT party_type FROM party"),
        run_execution=lambda sql, db, cat, **_kw: {
            "columns": ["party_type"], "rows": [["Individual"]], "answer": "1"},
    )
    ctx = tier2_rag_workflow(question="party types in the curated layer",
                             namespace="ns", kb_id="kb", deps=deps)
    assert ctx.degraded is None
    assert ctx.execution_result.get("rows") == [["Individual"]]


def test_phase3_noop_expand_short_circuits_to_degraded():
    # When expand() can add no fetchable table (returns the SAME slice), the loop
    # must bail to degrade immediately rather than re-judging an identical slice
    # for the remaining rounds. We count judge calls to prove the early-exit.
    slice_text = _slice([("normalized.rider_participant", "participant_sk")],
                        ["normalized.rider_participant"])
    calls = {"judge": 0}

    class _CountingJudge(_JudgeBuilder):
        def is_sufficient(self, *, slice_text, question):
            calls["judge"] += 1
            return False, ["normalized.participant"]  # never fetchable → no-op

    deps = PhaseDeps(
        router=_Router(["normalized.rider_participant"]),
        builder=_CountingJudge(slice_text, sufficient=False,
                               missing=["normalized.participant"]),
        generator=_Gen("SELECT 1"),
        run_execution=lambda *a, **_kw: {"rows": []},
    )
    ctx = tier2_rag_workflow(question="rider participants and roles",
                             namespace="ns", kb_id="kb", deps=deps)
    assert ctx.degraded == "phase3_max_rounds"
    # Exactly ONE judge call: round 1 judges, expand is a no-op, we bail — we do
    # NOT spend the remaining MAX_PHASE3_ROUNDS - 1 judge calls.
    assert calls["judge"] == 1, f"expected 1 judge call, got {calls['judge']}"
    assert "normalized.participant" in (ctx.degraded_detail or "")


def test_phase3_emits_slice_in_phase_result():
    # Phase 3 must emit the assembled slice JSON so the UI can view + download
    # the data that grounded SQL generation (todo item 2).
    slice_text = _slice([("db.customers", "id")], ["db.customers"])
    deps = PhaseDeps(
        router=_Router(["db.customers"]),
        builder=_Builder(slice_text),
        generator=_Gen("SELECT id FROM customers"),
        run_execution=lambda sql, db, cat, **_kw: {"columns": ["id"], "rows": [["1"]],
                                            "answer": "1 row"},
    )
    payloads = []

    def sink(phase, action, payload):
        payloads.append((phase, action, payload))

    tier2_rag_workflow(question="customers", namespace="ns", kb_id="kb",
                       deps=deps, phase_sink=sink)
    p3_results = [pl for (ph, a, pl) in payloads
                  if ph == 3 and a == "phase_result"]
    assert p3_results, "expected a Phase 3 phase_result event"
    assert p3_results[0].get("slice") == slice_text


def test_judge_fabrication_override_proceeds_on_invented_compound_table():
    # Regression for gt-row-00 ("insured party is also the policyholder"): the
    # judge invents non-existent COMPOUND join tables (holding_party, policy_owner)
    # as missing while life_participant (holding_id + party_id) already expresses
    # the relationship by self-join. expand() is a no-op (the invented names are
    # unfetchable). The fabrication guard must recognise the compound names as
    # invented AND the Phase-2-mapped table as present, then proceed to Phase 4.
    slice_text = _slice(
        [("normalized.life_participant", "holding_id"),
         ("normalized.life_participant", "party_id"),
         ("normalized.life_participant", "participant_sk"),
         ("normalized.party", "party_id")],
        ["normalized.life_participant", "normalized.party"],
    )
    deps = PhaseDeps(
        router=_Router(["normalized.life_participant", "normalized.party"]),
        builder=_JudgeBuilder(slice_text, sufficient=False,
                              missing=["holding_party", "policy_owner"]),
        generator=_Gen("SELECT 1"),
        run_execution=lambda sql, db, cat, **_kw: {
            "columns": ["c"], "rows": [["x"]], "answer": "ok"},
    )
    ctx = tier2_rag_workflow(question="policies where the party is also the party",
                             namespace="ns", kb_id="kb", deps=deps)
    # Guard fires only if Phase 2 mapped a term to a present table. When it does,
    # the invented-compound missing[] is overridden and we proceed (no degrade).
    if ctx.disambiguation:
        assert ctx.degraded is None, (
            "fabrication guard should override invented compound table names")
    # A genuinely-absent SINGLE-noun table must still degrade (contrast):
    slice2 = _slice([("normalized.rider_participant", "participant_sk")],
                    ["normalized.rider_participant"])
    deps2 = PhaseDeps(
        router=_Router(["normalized.rider_participant"]),
        builder=_JudgeBuilder(slice2, sufficient=False, missing=["participant"]),
        generator=_Gen("SELECT 1"),
        run_execution=lambda *a, **_kw: {"rows": []},
    )
    ctx2 = tier2_rag_workflow(question="rider participants and roles",
                              namespace="ns", kb_id="kb", deps=deps2)
    assert ctx2.degraded == "phase3_max_rounds", (
        "a plausible single-noun missing table must still degrade")
