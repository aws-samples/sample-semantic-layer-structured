"""Edit notebook 2 cells programmatically (never hand-edit the .ipynb JSON).

A task adds its cell source to the EDITS map below, then runs
``python scripts/_edit_nb2.py --cell N``. ``mode='append'`` adds to the existing
cell source; ``mode='replace'`` overwrites it. Idempotent for append via a
marker check: if the appended marker line is already present, it is not re-added.
"""
import argparse
import pathlib

import nbformat

NB = str(pathlib.Path(__file__).resolve().parents[1]
         / "notebooks" / "2_metadata_query_agent_ondemand_groundtruth_eval.ipynb")

# Source appended/replaced per cell index. Each task adds its entry here.
EDITS: dict = {
    7: {
        "mode": "append",
        "marker": "# @multiturn-cell7",
        "expect_contains": "groundtruth_dataset.json",
        "src": '''
# @multiturn-cell7  Multi-turn support: import the shared helpers (pure, no I/O)
# and report which rows are multi-turn so the run summary is honest.
try:
    from agents.shared.eval_multiturn import parse_multiturn_row
except ImportError:
    import sys
    sys.path.insert(0, '..')
    from agents.shared.eval_multiturn import parse_multiturn_row
_specs = [parse_multiturn_row(r, index=i) for i, r in enumerate(groundtruth)]
_mt = [s for s in _specs if len(s['turns']) > 1 or s['mode'] != 'scripted']
print(f"✓ {len(_specs)} scenario(s): {len(_mt)} multi-turn, {len(_specs) - len(_mt)} single-turn")
''',
    },
    13: {
        "mode": "replace",
        "expect_contains": "agent_invoker",
        "src": '''# @multiturn-cell13  Chat-stream invoker (Option A) + multi-turn dataset builder.
import json, time
from bedrock_agentcore.evaluation.runner.invoker_types import (
    AgentInvokerInput, AgentInvokerOutput)
from bedrock_agentcore.evaluation.runner.dataset_types import Dataset
try:
    from agents.shared.eval_multiturn import (
        parse_multiturn_row, build_chat_payload, build_scenarios,
        parse_chat_stream_sse, format_agent_output, run_key)
except ImportError:
    import sys; sys.path.insert(0, '..')
    from agents.shared.eval_multiturn import (
        parse_multiturn_row, build_chat_payload, build_scenarios,
        parse_chat_stream_sse, format_agent_output, run_key)

# Detect the simulation extra once; gate simulated scenarios on it.
try:
    import strands_evals  # noqa: F401
    SIMULATED_ENABLED = True
except ImportError:
    SIMULATED_ENABLED = False
print(f"simulated mode: {'ENABLED' if SIMULATED_ENABLED else 'DISABLED (scripted only)'}")

AGENT_RUNS = {}              # keyed by run_key(session_id, turn_idx)

def agent_invoker(invoker_input: AgentInvokerInput) -> AgentInvokerOutput:
    """Drive the agent CHAT-STREAM path so it reads+persists history itself.

    The SDK reuses one session_id across a scenario's turns; the chat entrypoint
    keys DDB history off the payload sessionId, so turn N+1 resolves turn N's
    clarification exactly as in production. We parse the SSE response, fold any
    clarification option labels into the judge-visible output, and record
    per-turn cost/latency keyed by (session_id, turn_idx).
    """
    sid = invoker_input.session_id
    # turn index derived from AGENT_RUNS so a notebook re-run (AGENT_RUNS reset above) is safe
    turn_idx = sum(1 for k in AGENT_RUNS if k.startswith(f"{sid}#turn"))
    message = (invoker_input.payload if isinstance(invoker_input.payload, str)
               else json.dumps(invoker_input.payload))
    payload = build_chat_payload(message=message, session_id=sid,
                                 ontology_id=EVAL_ID, turn_idx=turn_idx)
    start = time.time(); err = None; parsed = {}
    try:
        raw = _invoke_runtime(METADATA_QUERY_RUNTIME_ARN, sid,
                              json.dumps(payload).encode("utf-8"))
        parsed = parse_chat_stream_sse(raw.decode("utf-8", errors="replace"))
        err = parsed.get("error")
    except Exception as exc:  # noqa: BLE001
        err = str(exc); print(f"  ⚠ [{sid} t{turn_idx}] {err}")
    AGENT_RUNS[run_key(sid, turn_idx)] = {
        "scenario_session": sid, "turn_idx": turn_idx, "message": message,
        "answer": parsed.get("answer", ""), "agent_sql": parsed.get("sql", ""),
        "clarified": bool(parsed.get("clarification")),
        "rows": parsed.get("rows", []), "usage": parsed.get("usage", {}),
        "runtime_ms": parsed.get("runtime_ms"),
        "wall_clock_s": round(time.time() - start, 2), "invoke_error": err,
    }
    state = "clarify" if parsed.get("clarification") else ("answer" if parsed.get("sql") else "none")
    print(f"  {'OK' if err is None else 'XX'} [{sid} t{turn_idx}] "
          f"{AGENT_RUNS[run_key(sid, turn_idx)]['wall_clock_s']}s . {state}")
    return AgentInvokerOutput(agent_output=format_agent_output(parsed) or (err or ""))

_specs = [parse_multiturn_row(r, index=i) for i, r in enumerate(groundtruth)]
dataset = Dataset(scenarios=build_scenarios(
    _specs, ontology_id=EVAL_ID, simulated_enabled=SIMULATED_ENABLED))
EXPECTED_TRAJECTORY = ["execute_sql_query"]
print(f"✓ Dataset: {len(dataset.scenarios)} scenario(s) "
      f"({sum(1 for s in _specs if len(s['turns'])>1)} multi-turn)")
''',
    },
    15: {
        "mode": "replace",
        "expect_contains": "BatchEvaluationRunner",
        "src": '''# @multiturn-cell15  Batch runner: + Helpfulness evaluator, longer ingestion delay.
from bedrock_agentcore.evaluation.runner.batch.batch_evaluation_runner import (
    BatchEvaluationRunner,
)
from bedrock_agentcore.evaluation.runner.batch.batch_evaluation_models import (
    BatchEvaluationRunConfig, BatchEvaluatorConfig, CloudWatchDataSourceConfig,
)

SERVICE_NAME = f"{agent_id}.DEFAULT"
RUNTIME_LOG_GROUP = f"/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT"
SPANS_LOG_GROUP = "aws/spans"
print(f"SERVICE_NAME : {SERVICE_NAME}")
print(f"LOG GROUPS   : {SPANS_LOG_GROUP}, {RUNTIME_LOG_GROUP}")

batch_data_source = CloudWatchDataSourceConfig(
    service_names=[SERVICE_NAME],
    log_group_names=[SPANS_LOG_GROUP, RUNTIME_LOG_GROUP],
    ingestion_delay_seconds=300,   # multi-turn sessions emit more spans
)

ALL_EVALUATOR_IDS = [
    'Builtin.GoalSuccessRate',        # SESSION — assertions
    'Builtin.Correctness',            # TRACE   — expected_response
    'Builtin.Helpfulness',            # TRACE   — trajectory quality
    *CUSTOM_EVALUATOR_IDS,            # custom TRACE + SESSION judges
]

batch_config = BatchEvaluationRunConfig(
    batch_evaluation_name=f"query_gt_batch_{uuid.uuid4().hex[:8]}",
    description="Server-side built-in + custom ground-truth eval of the metadata query agent.",
    evaluator_config=BatchEvaluatorConfig(evaluator_ids=ALL_EVALUATOR_IDS),
    data_source=batch_data_source,
    max_concurrent_scenarios=3,   # synchronous agent, up to 900s/row
    polling_timeout_seconds=3600,
    polling_interval_seconds=30,
)

print(f"\\nBatch name : {batch_config.batch_evaluation_name}")
print(f"Evaluators : {ALL_EVALUATOR_IDS}")
print(f"Scenarios  : {len(dataset.scenarios)}")
print("Starting batch evaluation (invokes the agent per row, then evaluates server-side)...\\n")

batch_runner = BatchEvaluationRunner(region=region)
batch_result = batch_runner.run_dataset_evaluation(
    config=batch_config,
    dataset=dataset,
    agent_invoker=agent_invoker,
)
print(f"\\n✓ Batch status: {batch_result.status}")
if batch_result.agent_invocation_failures:
    print(f"⚠ Agent invocation failures: {len(batch_result.agent_invocation_failures)}")
''',
    },
    17: {
        "mode": "replace",
        "expect_contains": "query_batch_eval_",
        "src": '''# @multiturn-cell17  Per-turn trajectory results + preserved results JSON dump.
os.makedirs('../data/eval/results', exist_ok=True)
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
try:
    from agents.shared.eval_multiturn import group_runs_by_session
except ImportError:
    import sys; sys.path.insert(0, '..')
    from agents.shared.eval_multiturn import group_runs_by_session

print(f"Batch ID : {batch_result.batch_evaluation_id}")
print(f"ARN      : {batch_result.batch_evaluation_arn}")
print(f"Status   : {batch_result.status}")

# Map a session id (scenario_id + '-' + uuid) back to its scenario_id via the
# specs built in cell 13. Robust to dashes inside scenario_id (prefix match).
def _scenario_id_for_session(sess):
    for s in _specs:
        if sess and sess.startswith(s['scenario_id'] + '-'):
            return s['scenario_id']
    return sess or ''

# ── 1. Aggregate per-evaluator scores ────────────────────────────────────────
agg_rows = []
ev = batch_result.evaluation_results
if ev is not None:
    print(f"\\nSessions: completed={ev.number_of_sessions_completed} "
          f"failed={ev.number_of_sessions_failed} total={ev.total_number_of_sessions}")
    for es in (ev.evaluator_summaries or []):
        stats = es.statistics
        avg = (f"{stats.average_score:.3f}"
               if stats and stats.average_score is not None else None)
        agg_rows.append({
            'Evaluator': es.evaluator_id or 'unknown',
            'AvgScore': avg,
            'Evaluated': es.total_evaluated or 0,
            'Failed': es.total_failed or 0,
        })
else:
    print("\\n⚠ No aggregate evaluation_results — check job status / spans.")
df_agg = pd.DataFrame(agg_rows)

_EVAL_NAME = {ANSWER_FAITHFUL_ID: 'QueryAnswerFaithfulness',
              SQL_GROUNDED_ID: 'SqlGrounded',
              TOOL_ORDERING_ID: 'ToolCallOrdering'}

grouped = group_runs_by_session(AGENT_RUNS)

# ── 2. Per-query evaluator events (retry: output stream lags COMPLETED) ──
events = []
for _attempt in range(6):
    try:
        events = batch_runner.fetch_evaluation_events(batch_result)
        if events:
            break
    except (LookupError, ValueError) as exc:
        print(f"  per-query events not ready yet ({exc}); retrying in 20s...")
    time.sleep(20)

def _event_field(e, key):
    """Read an event field from the top level or the nested 'attributes' map."""
    if key in e:
        return e[key]
    return (e.get('attributes', {}) or {}).get(key)

event_rows = []
for e in events:
    sess = _event_field(e, 'session.id') or _event_field(e, 'gen_ai.session.id')
    turns = grouped.get(sess, [])
    first_msg = turns[0]['message'] if turns else ''
    name = _event_field(e, 'gen_ai.evaluation.name')
    event_rows.append({
        'scenario_id': _scenario_id_for_session(sess),
        'question': (first_msg or '')[:50],
        'evaluator': _EVAL_NAME.get(name, name),
        'score': _event_field(e, 'gen_ai.evaluation.score.value'),
        'label': _event_field(e, 'gen_ai.evaluation.score.label'),
        'explanation': (str(_event_field(e, 'gen_ai.evaluation.explanation') or ''))[:140],
    })
df_events = pd.DataFrame(event_rows)
print(f"\\nPer-query evaluator events: {len(df_events)}")

# ── 3. Per-turn trajectory table + clarification summary ─────────────────────
turn_rows = []
for sess, turns in grouped.items():
    sid = _scenario_id_for_session(sess)
    for t in turns:
        turn_rows.append({
            'scenario_id': sid,
            'turn': t.get('turn_idx'),
            'message': (t.get('message') or '')[:40],
            'clarified': t.get('clarified'),
            'has_sql': bool(t.get('agent_sql')),
            'wall_s': t.get('wall_clock_s'),
            'error': t.get('invoke_error'),
        })
df_agent = pd.DataFrame(turn_rows)
if not df_agent.empty:
    df_agent = df_agent.sort_values(['scenario_id', 'turn']).reset_index(drop=True)

print("\\n── Trajectory summary (per scenario) ──")
for sess, turns in grouped.items():
    sid = _scenario_id_for_session(sess)
    clar = [t.get('turn_idx') for t in turns if t.get('clarified')]
    final_sql = bool(turns[-1].get('agent_sql')) if turns else False
    print(f"  {sid}: {len(turns)} turn(s) · clarified_on={clar or 'none'} · "
          f"re_clarified={len(clar) > 1} · final_sql={final_sql}")

# ── 4. Agent cost/latency (sum over per-turn usage dicts) ────────────────────
_lat = [r['wall_clock_s'] for r in AGENT_RUNS.values() if r.get('wall_clock_s') is not None]
def _usage_sum(key):
    return sum(int((r.get('usage') or {}).get(key) or 0) for r in AGENT_RUNS.values())
agent_perf = {
    'turns': len(AGENT_RUNS),
    'sessions': len(grouped),
    'avg_wall_clock_s': round(sum(_lat) / len(_lat), 2) if _lat else None,
    'input_tokens': _usage_sum('inputTokens'),
    'output_tokens': _usage_sum('outputTokens'),
    'total_tokens': _usage_sum('totalTokens'),
}
print("\\n── Query agent cost/latency ──")
print(f"  Turns / sessions : {agent_perf['turns']} / {agent_perf['sessions']}")
print(f"  Avg wall-clock   : {agent_perf['avg_wall_clock_s']}s  (n={len(_lat)})")
print(f"  Agent tokens     : {agent_perf['total_tokens']} "
      f"(in={agent_perf['input_tokens']}, out={agent_perf['output_tokens']})")

# ── Persist everything (same filename pattern + keys notebook 4 reads) ───────
combined_file = f"../data/eval/results/query_batch_eval_{timestamp}.json"
combined = {
    'agent_id': agent_id,
    'runtime_arn': METADATA_QUERY_RUNTIME_ARN,
    'eval_id': EVAL_ID,
    'batch_evaluation_id': batch_result.batch_evaluation_id,
    'batch_evaluation_arn': batch_result.batch_evaluation_arn,
    'status': str(batch_result.status),
    'evaluator_ids': ALL_EVALUATOR_IDS,
    'custom_evaluators': {'QueryAnswerFaithfulness': ANSWER_FAITHFUL_ID,
                          'SqlGrounded': SQL_GROUNDED_ID,
                          'ToolCallOrdering': TOOL_ORDERING_ID},
    'aggregate_summaries': agg_rows,
    'per_query_events': event_rows,
    'agent_runs': list(AGENT_RUNS.values()),
    'agent_performance': agent_perf,
}
combined = _redact_account_ids(combined)
with open(combined_file, 'w') as f:
    json.dump(combined, f, indent=2, default=str)
print(f"\\n✓ Wrote {combined_file}")

print("\\n=== Aggregate per-evaluator scores ===")
display(df_agg)
print("=== Per-query evaluator scores ===")
display(df_events)
print("=== Per-turn trajectory ===")
display(df_agent)
''',
    },
}


def main() -> None:
    """Apply the EDITS entry for the requested cell index."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", type=int, required=True)
    args = ap.parse_args()
    nb = nbformat.read(NB, as_version=4)
    edit = EDITS[args.cell]
    cell = nb.cells[args.cell]
    expect = edit.get("expect_contains")
    if expect and expect not in cell.source:
        raise ValueError(
            f"cell {args.cell} does not contain expected marker {expect!r} — "
            f"wrong cell index? (refusing to edit)"
        )
    marker = edit.get("marker")
    if edit["mode"] == "append":
        if marker and marker in cell.source:
            print(f"= cell {args.cell} already has marker {marker!r}; skipping")
            return
        cell.source = cell.source.rstrip() + "\n" + edit["src"].strip() + "\n"
    else:
        cell.source = edit["src"].strip() + "\n"
    nbformat.write(nb, NB)
    print(f"✓ updated cell {args.cell}")


if __name__ == "__main__":
    main()
