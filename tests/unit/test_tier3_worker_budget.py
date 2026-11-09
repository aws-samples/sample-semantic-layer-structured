"""Unit tests for the Tier 3 per-round worker budget (both query agents).

The budget is the hard bound on ONE free-flowing worker round. The supervisor
already bounds the number of ROUNDS, but a round is a single call into a Strands
``Agent`` whose ReAct loop has no iteration cap — so a worker hitting a
non-convergent tool error retried forever. Measured on the 2026-08-07 nb12 BIRD
run: 123/500 rows died at the notebook's 900s client timeout, and session
``gt-row-497`` was still in ``supervisor round 1/3`` at 1100s after 157
translation calls and 66 Athena queries.

Both agents ship a parallel ``tier3`` package with the same budget semantics, so
the shared behaviour is parametrized over both modules.
"""
import pytest

from agents.metadata_query_agent import tier3 as meta_tier3
from agents.ontology_query_agent import tier3 as onto_tier3

ALL_MODULES = [meta_tier3, onto_tier3]
MODULE_IDS = ["metadata", "ontology"]


class _FakeClock:
    """A manually-advanced monotonic clock so wall-clock tests never sleep."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current fake time in seconds."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far to advance, in seconds.
        """
        self.now += seconds


def _clean_attempt(mod, *, answer="ok"):
    """A cleanly-executed WorkerAttempt for the given tier3 module.

    Handles the metadata (``sql=``) vs ontology (``query=``) field-name split.
    """
    if mod is meta_tier3:
        return mod.WorkerAttempt(answer=answer, sql="SELECT 1", results=[["v"]],
                                 columns=["c"])
    return mod.WorkerAttempt(answer=answer, query="SELECT ?x WHERE {}",
                             results=[["v"]], columns=["c"])


# --- WorkerBudget ------------------------------------------------------------
@pytest.mark.parametrize("mod", ALL_MODULES, ids=MODULE_IDS)
class TestWorkerBudget:
    def test_calls_under_the_cap_are_allowed(self, mod):
        # The healthy path must be untouched — a few retrieve/execute pairs.
        budget = mod.WorkerBudget(max_tool_calls=3)
        for _ in range(3):
            budget.check(tool_name="t")
        assert budget.tool_calls == 3
        assert budget.exceeded_reason is None

    def test_tool_call_cap_stops_the_round(self, mod):
        # This is the measured failure mode: hundreds of cheap calls in one round.
        budget = mod.WorkerBudget(max_tool_calls=2)
        budget.check(tool_name="t")
        budget.check(tool_name="t")
        with pytest.raises(mod.WorkerBudgetExceeded) as exc:
            budget.check(tool_name="execute_sparql")
        # The reason must name the bound AND the tool, so a production log line
        # identifies which loop spun without re-reading the code.
        assert "2-tool-call" in str(exc.value)
        assert "execute_sparql" in str(exc.value)
        assert budget.exceeded_reason == str(exc.value)

    def test_wall_clock_deadline_stops_the_round(self, mod):
        # The other half: few calls, but each slow enough to blow the budget. A
        # call cap alone would never fire here.
        clock = _FakeClock()
        budget = mod.WorkerBudget(max_tool_calls=1000, round_seconds=100.0,
                                  clock=clock)
        budget.check(tool_name="t")
        clock.advance(99.0)
        budget.check(tool_name="t")  # still inside the deadline
        clock.advance(2.0)
        with pytest.raises(mod.WorkerBudgetExceeded) as exc:
            budget.check(tool_name="t")
        assert "wall-clock" in str(exc.value)

    def test_budget_error_is_a_base_exception_not_an_exception(self, mod):
        # LOAD-BEARING: Strands' tool decorator and tool executor both catch
        # ``Exception`` and hand the text back to the model as a tool RESULT, so
        # an Exception-derived budget error would be swallowed and the runaway
        # ReAct loop would simply continue. Only BaseException unwinds the call.
        assert issubclass(mod.WorkerBudgetExceeded, BaseException)
        assert not issubclass(mod.WorkerBudgetExceeded, Exception)

    def test_elapsed_tracks_the_clock(self, mod):
        clock = _FakeClock()
        budget = mod.WorkerBudget(clock=clock)
        clock.advance(12.5)
        assert budget.elapsed() == pytest.approx(12.5)

    def test_defaults_are_bounded_below_the_client_timeout(self, mod):
        # 3 rounds must fit inside the eval client's 900s invoke timeout with
        # room for Tier 2 (~75s) and the judge calls, or the bound is pointless.
        assert mod.DEFAULT_MAX_TOOL_CALLS < 157  # the measured runaway count
        assert 3 * mod.DEFAULT_ROUND_SECONDS + 75 < 900


# --- supervisor integration --------------------------------------------------
@pytest.mark.parametrize("mod", ALL_MODULES, ids=MODULE_IDS)
class TestSupervisorHandlesBudgetStops:
    def test_budget_stop_retries_instead_of_aborting_tier3(self, mod):
        # A budget stop is an EXPECTED outcome, not a crash. Before the fix a
        # raise on round 1 was fail-soft (ran=False) and abandoned Tier 3
        # entirely; now the round is treated as unclean and the loop retries.
        tasks = []
        attempts = {"n": 0}

        def worker(task):
            tasks.append(task)
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise mod.WorkerBudgetExceeded("budget: 40 tool calls in 12s")
            return _clean_attempt(mod)

        def judge(question, attempt):
            return mod.JudgeVerdict(sufficient=True)

        out = mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=3)
        assert out.ran is True
        assert out.accepted is True      # round 2 recovered
        assert out.rounds == 2
        assert attempts["n"] == 2

    def test_budget_guidance_tells_the_worker_not_to_repeat_the_shape(self, mod):
        # The retry must be DIRECTED. The measured errors were non-convergent
        # (Ontop rejecting the query shape), so re-emitting the same shape burns
        # another whole round for nothing.
        tasks = []

        def worker(task):
            tasks.append(task)
            if len(tasks) == 1:
                raise mod.WorkerBudgetExceeded("exceeded its 40-tool-call budget")
            return _clean_attempt(mod)

        def judge(question, attempt):
            return mod.JudgeVerdict(sufficient=True)

        mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=3)
        assert "40-tool-call" in tasks[1]
        assert "same query shape" in tasks[1]

    def test_every_round_budget_stopped_keeps_the_tier2_answer(self, mod):
        # Fail-soft end state: Tier 3 never fabricates. If all rounds are cut off
        # the caller must keep Tier 2's honest degraded answer.
        def worker(task):
            raise mod.WorkerBudgetExceeded("stopped")

        def judge(question, attempt):  # pragma: no cover — never reached
            raise AssertionError("judge must not run on a budget-stopped round")

        out = mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=2)
        assert out.accepted is False
        assert out.rounds == 2
        assert out.ran is True

    def test_budget_stop_does_not_consult_the_judge(self, mod):
        # A stopped round produced no result to grade; calling the judge would
        # spend a model call on nothing.
        judged = {"n": 0}
        attempts = {"n": 0}

        def worker(task):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise mod.WorkerBudgetExceeded("stopped")
            return _clean_attempt(mod)

        def judge(question, attempt):
            judged["n"] += 1
            return mod.JudgeVerdict(sufficient=True)

        mod.run_supervisor_loop("q", "ctx", worker, judge, max_rounds=3)
        assert judged["n"] == 1  # only the clean round 2 was judged
