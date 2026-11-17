"""Tier 3 worker budget (metadata query agent) — hard bound on ONE worker round.

:mod:`.supervisor` bounds the number of worker ROUNDS (``DEFAULT_MAX_ROUNDS``),
but a round is a single call into a Strands ``Agent``, and a Strands agent's
ReAct loop has no iteration cap of its own. So a worker that keeps getting a
*retryable-looking* tool error will keep retrying inside one round, forever.

That is not hypothetical — it was measured on the SIBLING (ontology / VKG) agent,
whose Tier 3 driver has the identical shape. On the 2026-08-07 nb12 BIRD run, 123
of 500 rows (~25%) died at the notebook's ``urlopen(..., timeout=900)``.
Reconstructing session ``gt-row-497`` from CloudWatch: Tier 2 Phase 5 failed
translation at 33s, its repair failed at 73s, ``[tier3] supervisor round 1/3``
started at 73.9s — and at 1100s it was STILL in round 1, having issued 157
translation calls and 66 Athena queries. It never reached round 2. Every
individual operation was fast (Athena 2-4s); the COUNT was the problem, driven by
non-convergent errors that failed identically on every retry. This agent's worker
can spin the same way on a repeatedly-rejected SQL, so it carries the same bound.

Two bounds, because either alone leaves a hole:

  * ``max_tool_calls`` — catches the fast-spin case above, where hundreds of
    cheap calls burn the wall clock.
  * ``deadline`` (wall-clock) — catches the slow-spin case, where a handful of
    genuinely slow calls blow the budget without ever hitting the call cap.

Enforcement detail that matters: raising a plain ``Exception`` inside a ``@tool``
does NOT stop a Strands agent. Both ``DecoratedFunctionTool.stream`` and the tool
executor catch ``Exception`` and hand the text back to the model as a tool
*result* — i.e. the model sees "your tool failed" and cheerfully tries again,
which is exactly the loop we are trying to break. :class:`WorkerBudgetExceeded`
therefore derives from ``BaseException`` so it propagates through the SDK's
handlers and unwinds the agent call.

The supervisor treats a budget stop as an ordinary unclean attempt: the round
ends with an ``error``, the judge is skipped, and the loop either retries with
guidance or exhausts and keeps Tier 2's honest degraded answer. Fail-soft, never
a fabricated answer.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# One round's tool-call ceiling. The healthy Tier 3 worker path is a handful of
# retrieve/execute pairs; the pathological session above issued 157 tool calls in
# a single round. 40 leaves generous room for legitimate multi-step reasoning
# while cutting the runaway case off ~4x sooner than the client timeout.
DEFAULT_MAX_TOOL_CALLS = 40

# One round's wall-clock ceiling, in seconds. Chosen against the eval client's
# 900s invoke timeout: Tier 2 spends up to ~75s before escalating, so a 240s
# round lets all 3 rounds finish (75 + 3*240 = 795s) inside 900s with headroom
# for the judge calls and the final answer render.
DEFAULT_ROUND_SECONDS = 240.0


class WorkerBudgetExceeded(BaseException):
    """Raised inside a worker tool when its round's budget is exhausted.

    Derives from ``BaseException``, NOT ``Exception``, on purpose: the Strands
    tool decorator and tool executor both catch ``Exception`` and return it to
    the model as a tool result, so an ``Exception`` here would be swallowed and
    the runaway ReAct loop would simply continue. See the module docstring.
    """


class WorkerBudget:
    """Per-round tool-call and wall-clock budget for one Tier 3 worker attempt.

    A fresh instance is created per round by the driver. Every budgeted tool
    calls :meth:`check` on entry, so the bound is enforced at the only place the
    worker can actually spend real time or money.
    """

    def __init__(
        self,
        *,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        round_seconds: float = DEFAULT_ROUND_SECONDS,
        clock: Optional[object] = None,
    ) -> None:
        """Start the budget clock.

        Args:
            max_tool_calls: Maximum budgeted tool invocations for this round.
            round_seconds: Maximum wall-clock seconds for this round.
            clock: Optional zero-arg monotonic-seconds callable, for tests.
                Defaults to :func:`time.monotonic`.
        """
        self.max_tool_calls = max_tool_calls
        self.round_seconds = round_seconds
        self._clock = clock or time.monotonic
        self.started_at: float = self._clock()
        self.tool_calls: int = 0
        # The reason this budget stopped the round, for the supervisor's error
        # text and the retry guidance. None while the budget is still healthy.
        self.exceeded_reason: Optional[str] = None

    def elapsed(self) -> float:
        """Return seconds elapsed since this round started."""
        return self._clock() - self.started_at

    def check(self, *, tool_name: str) -> None:
        """Charge one tool call against the budget, raising if it is exhausted.

        Called at the TOP of every budgeted worker tool, so the ceiling is
        enforced before the call spends time or tokens.

        Args:
            tool_name: The tool being invoked, for the log line and the reason.

        Raises:
            WorkerBudgetExceeded: If this round has exhausted either its
                tool-call ceiling or its wall-clock deadline.
        """
        elapsed = self.elapsed()
        if elapsed >= self.round_seconds:
            self.exceeded_reason = (
                f"Tier 3 worker round exceeded its {self.round_seconds:.0f}s "
                f"wall-clock budget after {self.tool_calls} tool call(s) "
                f"(stopped at {tool_name})"
            )
            logger.warning("[tier3] %s", self.exceeded_reason)
            raise WorkerBudgetExceeded(self.exceeded_reason)
        if self.tool_calls >= self.max_tool_calls:
            self.exceeded_reason = (
                f"Tier 3 worker round exceeded its {self.max_tool_calls}-tool-call "
                f"budget in {elapsed:.0f}s (stopped at {tool_name})"
            )
            logger.warning("[tier3] %s", self.exceeded_reason)
            raise WorkerBudgetExceeded(self.exceeded_reason)
        self.tool_calls += 1
