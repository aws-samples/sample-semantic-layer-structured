"""Pure, I/O-free helpers for the multi-turn / trajectory eval harness.

Extracted from notebook 2 so the SSE parsing + scenario shaping logic is unit-
testable without invoking a runtime. No boto3, no DDB, no network — the notebook
supplies the transport (the M2M-authed HTTPS call) and passes the raw body here.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional


def parse_chat_stream_sse(body: str) -> Dict[str, Any]:
    """Parse an AG-UI SSE chat-stream body into a flat result dict.

    The chat-stream entrypoint yields ``data: {json}\\n\\n`` envelopes
    (``message_chunk`` deltas, then a terminal ``run_finished`` carrying
    ``totals``, or a ``run_error``). We concat the deltas into ``answer`` and
    lift sql/rows/usage/clarification/provenance out of ``totals``.

    Args:
        body: The full decoded SSE response body.

    Returns:
        ``{answer, sql, rows, usage, runtime_ms, clarification, provenance, error}``.
    """
    answer_parts: List[str] = []
    totals: Dict[str, Any] = {}
    error: Optional[str] = None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue  # tolerate keep-alive / non-JSON frames
        etype = ev.get("type")
        if etype == "message_chunk":
            answer_parts.append(ev.get("delta", "") or "")
        elif etype == "run_finished":
            totals = ev.get("totals", {}) or {}
        elif etype == "run_error":
            error = ev.get("error", "") or "run_error"
    return {
        "answer": "".join(answer_parts),
        "sql": totals.get("sql", "") or "",
        "rows": totals.get("rows", []) or [],
        "usage": totals.get("usage", {}) or {},
        "runtime_ms": totals.get("runtimeMs"),
        "clarification": totals.get("clarification"),
        # Answer-source label (Step 1) — lets the harness assert a question
        # routed to the expected tier (governed_metric|semantic_sql|vkg|advisory).
        "provenance": totals.get("provenance"),
        "error": error,
    }


SINGLE_TURN_MODE = "scripted"


def run_key(session_id: str, turn_idx: int) -> str:
    """Per-turn key for the AGENT_RUNS dict (multi-turn reuses one session_id)."""
    return f"{session_id}#turn{turn_idx}"


def scenario_id_for(row: Dict[str, Any], index: int) -> str:
    """Explicit multiturn.scenario_id if present, else the legacy gt-row-NN id."""
    mt = row.get("multiturn") or {}
    return mt.get("scenario_id") or f"gt-row-{index:02d}"


def parse_multiturn_row(row: Dict[str, Any], *, index: int) -> Dict[str, Any]:
    """Normalize a ground-truth row into a uniform multi-turn spec.

    A row with no ``multiturn`` block becomes a single scripted turn (the legacy
    behaviour). Returns ``{mode, scenario_id, turns, trajectory_assertions,
    actor_profile, max_turns, expected_answer, expected_tier}``. ``turns`` is a
    list of ``{input, ...}`` dicts. ``expected_tier`` is the provenance tier the
    question should route to (``governed_metric|semantic_sql|vkg|advisory``);
    it defaults to ``semantic_sql`` for legacy data-query rows that omit it.

    Args:
        row: One ground-truth dataset row.
        index: Row index (for the fallback scenario id).
    """
    mt = row.get("multiturn") or {}
    scenario_id = scenario_id_for(row, index)
    mode = mt.get("mode", SINGLE_TURN_MODE)
    if "multiturn" in row:
        turns = mt.get("turns")
        if not turns:
            raise ValueError(
                f"scenario {scenario_id!r} has a 'multiturn' block but no "
                f"non-empty 'turns' list"
            )
    else:
        # Legacy single-turn row: synthesize one turn from the question.
        turns = [{"input": row["Natural_Language_Question"]}]
    return {
        "mode": mode,
        "scenario_id": scenario_id,
        "turns": turns,
        "trajectory_assertions": mt.get("trajectory_assertions") or [],
        "actor_profile": mt.get("actor_profile"),
        "max_turns": mt.get("max_turns", 4),
        "expected_answer": row.get("Expected_Answer", ""),
        # Expected provenance tier for the correct-tier assertion (Step 5). Legacy
        # data-query rows omit it → default semantic_sql (today's behavior).
        "expected_tier": row.get("Expected_Tier", "semantic_sql"),
    }


def build_chat_payload(*, message: str, session_id: str, ontology_id: str,
                       turn_idx: int, user_id: str = "eval") -> Dict[str, Any]:
    """Build the chat-stream payload (the path that reads+persists history).

    The chat entrypoint reads ``message`` (NOT ``question``) and dispatches to
    ``_chat_stream`` because ``turnId`` is present. A stable ``sessionId`` across
    a scenario's turns is what lets turn N+1 resolve turn N's clarification.

    Args:
        message: The user's message for this turn.
        session_id: Stable per-scenario session id (reused across turns).
        ontology_id: Semantic-layer id passed through as ``ontologyId``.
        turn_idx: 0-based turn index within the scenario.
        user_id: Caller identity for memory scoping (default ``"eval"``).
    """
    return {
        "turnId": f"{session_id}-t{turn_idx}-{uuid.uuid4().hex[:8]}",
        "message": message,
        "sessionId": session_id,
        "ontologyId": ontology_id,
        "mode": "semantic-rag",
        "userId": user_id,
    }


FINAL_ANSWER_ASSERTION_PREFIX = "The conversation's final answer matches: "


def build_trajectory_assertions(spec: Dict[str, Any]) -> List[str]:
    """SESSION-judge assertions for the whole conversation.

    Combines the row's explicit ``trajectory_assertions`` with a final-answer
    assertion so the SESSION judges score both the path and the destination.

    Because the SESSION evaluators can only see ``{context}`` + ``{assertions}``
    (the batch API forbids ``{expected_response}`` at session level — that
    placeholder is TRACE-only), the expected final answer is threaded in here as
    an assertion prefixed with ``FINAL_ANSWER_ASSERTION_PREFIX`` so the
    FinalAnswerFaithfulness judge can locate it.

    Per-turn ``expect_clarification`` flags are wired in as a FALLBACK: when a row
    carries the structured flags but no curated ``trajectory_assertions`` prose,
    we synthesize concise per-turn assertions so the structured ground truth is
    not silently dropped. When curated prose is present it already covers this, so
    we don't duplicate it.

    Args:
        spec: A spec dict from :func:`parse_multiturn_row`.
    """
    out = list(spec.get("trajectory_assertions") or [])
    if not out:
        # No curated prose — derive assertions from the per-turn expect_clarification
        # flags so the structured field still reaches the SESSION judge.
        for i, turn in enumerate(spec.get("turns") or []):
            if "expect_clarification" not in turn:
                continue
            if turn["expect_clarification"]:
                out.append(
                    f"On turn {i + 1} the agent asked a clarifying question "
                    f"rather than answering directly."
                )
            else:
                out.append(
                    f"On turn {i + 1} the agent answered directly without "
                    f"asking a clarifying question."
                )
    if spec.get("expected_answer"):
        out.append(f"{FINAL_ANSWER_ASSERTION_PREFIX}{spec['expected_answer']}")
    return out


def format_agent_output(parsed: Dict[str, Any]) -> str:
    """Render a parsed turn result as judge-visible text.

    For a normal turn this is just the answer. For a CLARIFICATION turn the
    structured option labels are folded into the text (prefixed with a
    ``CLARIFICATION`` marker) so a SESSION judge can verify *which* options were
    offered and whether they stayed stable across re-asks — the option labels
    otherwise live only in the structured payload, invisible to the judge.

    Args:
        parsed: One result dict from :func:`parse_chat_stream_sse`.
    """
    if parsed.get("error") and not parsed.get("answer"):
        return f"[error] {parsed['error']}"
    text = parsed.get("answer", "") or ""
    clar = parsed.get("clarification")
    if clar and isinstance(clar, dict):
        labels = [o.get("label", "") for o in (clar.get("options") or [])
                  if isinstance(o, dict)]
        if labels:
            text = (f"{text}\n\n[CLARIFICATION] The agent asked the user to choose "
                    f"among: {', '.join(labels)}")
    return text


def build_scenarios(specs: List[Dict[str, Any]], *, ontology_id: str,
                    simulated_enabled: bool) -> List[Any]:
    """Map normalized specs to SDK scenario objects (scripted | simulated).

    Imports SDK types lazily so the module stays import-light for unit tests that
    don't need them. Simulated scenarios are dropped (with a printed warning) when
    ``simulated_enabled`` is False so scripted runs stay dependency-free.

    Args:
        specs: Output of :func:`parse_multiturn_row` per row.
        ontology_id: Semantic-layer id, stamped into scenario metadata.
        simulated_enabled: Whether the simulation extra (``pip install
            'bedrock-agentcore[simulation]'``, importable as ``strands_evals``)
            is present.
    """
    from bedrock_agentcore.evaluation.runner.dataset_types import (
        PredefinedScenario, Turn, SimulatedScenario, ActorProfile,
    )
    out: List[Any] = []
    for spec in specs:
        assertions = build_trajectory_assertions(spec)
        if spec["mode"] == "simulated":
            if not simulated_enabled:
                print(f"  ⚠ skipping simulated scenario {spec['scenario_id']} "
                      f"(install bedrock-agentcore[simulation])")
                continue
            ap = spec["actor_profile"] or {}
            out.append(SimulatedScenario(
                scenario_id=spec["scenario_id"],
                actor_profile=ActorProfile(traits=ap.get("traits", {}),
                                           context=ap.get("context", ""),
                                           goal=ap.get("goal", "")),
                input=spec["turns"][0]["input"],
                max_turns=spec["max_turns"],
                assertions=assertions,
                metadata={"ontologyId": ontology_id},
            ))
        else:
            # Bind the expected answer to the FINAL turn only. expected_response is
            # the TRACE-level ground truth; for a multi-turn conversation only the
            # last turn produces the answer, so earlier (clarification) turns get no
            # expected_response and are never scored against the final answer.
            spec_turns = spec["turns"]
            last_idx = len(spec_turns) - 1
            expected_answer = spec.get("expected_answer") or None
            turns = [
                Turn(
                    input=t["input"],
                    expected_response=(expected_answer if i == last_idx else None),
                )
                for i, t in enumerate(spec_turns)
            ]
            out.append(PredefinedScenario(
                scenario_id=spec["scenario_id"],
                turns=turns,
                assertions=assertions,
                expected_trajectory=["execute_sql_query"],
                metadata={"ontologyId": ontology_id},
            ))
    return out


def group_runs_by_session(runs: Dict[str, Dict[str, Any]]
                          ) -> Dict[str, List[Dict[str, Any]]]:
    """Group AGENT_RUNS (keyed by run_key) into per-session, turn-ordered lists.

    Multi-turn scenarios reuse one ``session_id`` across turns, so AGENT_RUNS is
    keyed by ``run_key(session_id, turn_idx)``. This regroups the flat dict into
    ``{session_id: [turn_record, ...]}`` ordered by ``turn_idx`` for display.

    Args:
        runs: The flat AGENT_RUNS dict (run_key -> per-turn record).

    Raises:
        KeyError: if a record lacks ``scenario_session`` or ``turn_idx`` (an
            invariant every AGENT_RUNS record satisfies — fail loudly if not).
    """
    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for rec in runs.values():
        by_session.setdefault(rec["scenario_session"], []).append(rec)
    for recs in by_session.values():
        recs.sort(key=lambda r: r["turn_idx"])
    return by_session
