"""Unit tests for the Tier 3 supervisor loop (both query agents).

The loop core is dependency-injected (``worker_fn`` / ``judge_fn``), so these
tests drive the REAL control flow with in-memory fakes — no Bedrock, no gateway.
Both agents ship a parallel ``tier3`` package with the same loop semantics; the
shared behaviour is parametrized over both modules, and the small field-naming
difference (metadata ``WorkerAttempt.sql`` vs ontology ``.query``) is covered by
a per-module helper.
"""
import pytest

from agents.metadata_query_agent import tier3 as meta_tier3
from agents.ontology_query_agent import tier3 as onto_tier3


# --- helpers ----------------------------------------------------------------
def _clean_attempt(mod, *, answer="ok", rows=None):
    """A cleanly-executed WorkerAttempt for the given tier3 module.

    Handles the metadata (``sql=``) vs ontology (``query=``) field-name split so
    the shared tests stay module-agnostic.
    """
    rows = rows if rows is not None else [["v"]]
    if mod is meta_tier3:
        return mod.WorkerAttempt(answer=answer, sql="SELECT 1", results=rows,
                                 columns=["c"])
    return mod.WorkerAttempt(answer=answer, query="SELECT ?x WHERE {}", results=rows,
                             columns=["c"])


def _failed_attempt(mod, *, error="boom"):
    return mod.WorkerAttempt(answer="", error=error)


ALL_MODULES = [meta_tier3, onto_tier3]
MODULE_IDS = ["metadata", "ontology"]


# --- parse_judge_verdict ----------------------------------------------------
@pytest.mark.parametrize("mod", ALL_MODULES, ids=MODULE_IDS)
class TestParseJudgeVerdict:
    def test_parses_plain_dict(self, mod):
        v = mod.parse_judge_verdict({"sufficient": True, "guidance": ""})
        assert v.sufficient is True

    def test_parses_json_string(self, mod):
        v = mod.parse_judge_verdict('{"sufficient": false, "guidance": "use table X"}')
        assert v.sufficient is False
        assert v.guidance == "use table X"

    def test_parses_fenced_json_with_prose(self, mod):
        raw = 'Here is my verdict:\n```json\n{"sufficient": true}\n```\nDone.'
        assert mod.parse_judge_verdict(raw).sufficient is True

    def test_string_bool_forms(self, mod):
        assert mod.parse_judge_verdict('{"sufficient": "yes"}').sufficient is True
        assert mod.parse_judge_verdict('{"sufficient": "no"}').sufficient is False

    def test_unparseable_defaults_to_not_sufficient(self, mod):
        # The safe default: never accept an answer the judge didn't bless.
        assert mod.parse_judge_verdict("not json at all").sufficient is False
        assert mod.parse_judge_verdict("").sufficient is False
        assert mod.parse_judge_verdict(None).sufficient is False
        assert mod.parse_judge_verdict(42).sufficient is False

    def test_guidance_falls_back_to_gap_key(self, mod):
        v = mod.parse_judge_verdict({"sufficient": False, "gap": "missing join"})
        assert v.guidance == "missing join"


# --- run_supervisor_loop ----------------------------------------------------
@pytest.mark.parametrize("mod", ALL_MODULES, ids=MODULE_IDS)
class TestSupervisorLoop:
    def test_judge_accept_ends_loop_early(self, mod):
        calls = {"worker": 0, "judge": 0}

        def worker(task):
            calls["worker"] += 1
            return _clean_attempt(mod)

        def judge(question, attempt):
            calls["judge"] += 1
            return mod.JudgeVerdict(sufficient=True)

        out = mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=3)
        assert out.accepted is True
        assert out.ran is True
        assert out.rounds == 1
        assert calls["worker"] == 1  # accepted on round 1 — no further attempts
        assert calls["judge"] == 1

    def test_loop_bound_respected_and_not_accepted(self, mod):
        calls = {"worker": 0}

        def worker(task):
            calls["worker"] += 1
            return _clean_attempt(mod)

        # Judge never satisfied → loop must stop at max_rounds, accepted False.
        def judge(question, attempt):
            return mod.JudgeVerdict(sufficient=False, guidance="try again")

        out = mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=2)
        assert out.accepted is False
        assert out.ran is True
        assert out.rounds == 2
        assert calls["worker"] == 2  # exactly max_rounds, no overrun

    def test_guidance_is_threaded_into_next_worker_round(self, mod):
        seen_tasks = []

        def worker(task):
            seen_tasks.append(task)
            return _clean_attempt(mod)

        verdicts = iter([
            mod.JudgeVerdict(sufficient=False, guidance="SPECIFIC-NEXT-STEP"),
            mod.JudgeVerdict(sufficient=True),
        ])

        def judge(question, attempt):
            return next(verdicts)

        out = mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=3)
        assert out.accepted is True
        assert out.rounds == 2
        # Round 2's task must carry the judge's guidance from round 1.
        assert "SPECIFIC-NEXT-STEP" in seen_tasks[1]

    def test_failed_attempt_retries_without_calling_judge(self, mod):
        judged = {"n": 0}

        attempts = iter([_failed_attempt(mod, error="UNRESOLVED_COLUMN"),
                         _clean_attempt(mod)])

        def worker(task):
            return next(attempts)

        def judge(question, attempt):
            judged["n"] += 1
            return mod.JudgeVerdict(sufficient=True)

        out = mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=3)
        assert out.accepted is True
        assert out.rounds == 2
        # The judge is only consulted on the CLEAN attempt, not the failed one.
        assert judged["n"] == 1

    def test_worker_raises_on_first_round_is_fail_soft(self, mod):
        def worker(task):
            raise RuntimeError("bedrock down")

        def judge(question, attempt):  # pragma: no cover — never reached
            raise AssertionError("judge must not run when worker never produced an attempt")

        out = mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=3)
        assert out.ran is False
        assert out.accepted is False
        assert "raised" in out.reason

    def test_worker_raises_on_later_round_keeps_tier2(self, mod):
        attempts = [_clean_attempt(mod)]

        def worker(task):
            if attempts:
                return attempts.pop()
            raise RuntimeError("transient")

        def judge(question, attempt):
            return mod.JudgeVerdict(sufficient=False, guidance="again")

        out = mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=3)
        assert out.ran is True          # a clean attempt did happen
        assert out.accepted is False    # but nothing was accepted → keep Tier 2

    def test_usage_accumulates_across_rounds(self, mod):
        def worker(task):
            a = _clean_attempt(mod)
            a.usage = {"totalTokens": 100}
            return a

        verdicts = iter([mod.JudgeVerdict(sufficient=False, guidance="g"),
                         mod.JudgeVerdict(sufficient=True)])

        def judge(question, attempt):
            return next(verdicts)

        out = mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=3)
        assert out.usage.get("totalTokens") == 200  # two rounds × 100


# --- build_worker_task -------------------------------------------------------
@pytest.mark.parametrize("mod", ALL_MODULES, ids=MODULE_IDS)
class TestBuildWorkerTask:
    def test_includes_question_and_failure_context(self, mod):
        task = mod.build_worker_task("How many riders?", "phase1_empty degrade",
                                     guidance="", prior=None)
        assert "How many riders?" in task
        assert "phase1_empty degrade" in task

    def test_prior_attempt_and_guidance_included(self, mod):
        prior = _failed_attempt(mod, error="TABLE_NOT_FOUND")
        task = mod.build_worker_task("q", "ctx", guidance="use the trips table",
                                     prior=prior)
        assert "TABLE_NOT_FOUND" in task
        assert "use the trips table" in task
