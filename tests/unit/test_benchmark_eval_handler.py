"""Unit tests for the codeBased exec-metrics evaluator Lambda.

The handler (``cdk/lib/stacks/backend/benchmark-eval-handler/index.py``) scores a
text-to-SQL trace against ground truth on Athena and returns a SINGLE AgentCore
codeBased-evaluator result. The SAME handler is deployed as THREE Lambda functions
(EX / R-VES / Soft-F1), each with ``EVAL_METRIC`` in its environment; the handler
reads ``os.environ["EVAL_METRIC"]`` and falls back to ``event["evaluatorName"]``
only when the env var is absent (the direct-invoke / documented-schema path).

These tests pin the PURE, AWS-free pieces:
  * ``_metric_for_name``          — evaluator name → metric key.
  * ``_extract_predicted_sql``    — walk OTel sessionSpans → predicted SQL.
  * ``_extract_gold``             — walk evaluationReferenceInputs → gold payload.
Plus two end-to-end happy/sad paths with a fake Athena + monkeypatched
``run_sql`` so the handler's branch/scoring logic is exercised without AWS.

The handler lives under a bundled-Lambda asset dir (not an importable package
path) and its name ``index.py`` collides with other handlers, so it is loaded by
file path under a UNIQUE module name. The handler's own ``sys.path`` insert makes
its sibling vendored ``exec_metrics`` / ``athena_bench`` copies importable.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

_HANDLER_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "cdk",
        "lib",
        "stacks",
        "backend",
        "benchmark-eval-handler",
    )
)
_HANDLER_PATH = os.path.join(_HANDLER_DIR, "index.py")


@pytest.fixture(scope="module")
def handler():
    """Load the benchmark-eval handler module from its file path."""
    spec = importlib.util.spec_from_file_location("benchmark_eval_index", _HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clear_eval_metric_env(monkeypatch):
    """Ensure EVAL_METRIC is unset by default so evaluatorName-fallback tests are
    deterministic. The three deployed functions each set EVAL_METRIC in their
    Lambda environment; tests that exercise that path set it explicitly.
    """
    monkeypatch.delenv("EVAL_METRIC", raising=False)


# ── _metric_for_name ──────────────────────────────────────────────────────────


def test_metric_for_name_maps_all_three(handler):
    """Evaluator names map to their metric key by canonical substring."""
    assert handler._metric_for_name("bird_ExecutionAccuracy") == "ex"
    assert handler._metric_for_name("bird_RewardVES") == "rves"
    assert handler._metric_for_name("bird_SoftF1") == "soft_f1"


def test_metric_for_name_is_separator_and_case_insensitive(handler):
    """Hyphens / underscores / case do not defeat the mapping."""
    assert handler._metric_for_name("Execution-Accuracy") == "ex"
    assert handler._metric_for_name("reward_ves") == "rves"
    assert handler._metric_for_name("soft-f1-vkg") == "soft_f1"


def test_metric_for_name_unknown_raises(handler):
    """An unrecognized evaluator name fails loudly (no silent default)."""
    with pytest.raises(ValueError):
        handler._metric_for_name("SomethingElse")


# ── _extract_predicted_sql ──────────────────────────────────────────────────


def test_extract_predicted_sql_from_tool_span(handler):
    """A SQL-executor tool span yields its SQL argument."""
    spans = [
        {
            "name": "invoke_agent",
            "attributes": {"gen_ai.operation.name": "invoke_agent"},
        },
        {
            "name": "execute_tool execute_sql_query",
            "attributes": {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "execute_sql_query",
                # tool input arrives as a JSON-string arguments blob
                "gen_ai.tool.input": json.dumps(
                    {"sql": "SELECT count(*) FROM schools"}
                ),
            },
        },
    ]
    assert handler._extract_predicted_sql(spans) == "SELECT count(*) FROM schools"


def test_extract_predicted_sql_from_input_messages(handler):
    """SQL embedded in gen_ai.input.messages of a tool span is recovered."""
    spans = [
        {
            "attributes": {
                "gen_ai.tool.name": "execute_athena_query",
                "gen_ai.input.messages": json.dumps(
                    [{"role": "user", "content": [{"text": "run SELECT a FROM t"}]}]
                ),
                # a nested 'sql' key anywhere is the strongest signal
                "gen_ai.request.arguments": json.dumps({"sql": "SELECT a FROM t"}),
            }
        }
    ]
    assert handler._extract_predicted_sql(spans) == "SELECT a FROM t"


def test_extract_predicted_sql_empty_when_no_spans(handler):
    """No spans → empty string (a missing answer, not an error)."""
    assert handler._extract_predicted_sql([]) == ""


def test_extract_predicted_sql_empty_when_no_sql(handler):
    """Spans without any SQL yield empty string."""
    spans = [
        {
            "name": "invoke_agent",
            "attributes": {"gen_ai.operation.name": "invoke_agent"},
        }
    ]
    assert handler._extract_predicted_sql(spans) == ""


# ── _extract_gold ─────────────────────────────────────────────────────────────


def test_extract_gold_from_expected_response(handler):
    """expectedResponse.text = json.dumps({...}) → parsed gold dict."""
    payload = {
        "gold_sql": "SELECT 1",
        "db_id": "bird_cs",
        "catalog": "s3tablescatalog/b",
    }
    refs = [
        {
            "context": {"spanContext": {"sessionId": "s", "traceId": "t"}},
            "assertions": [{"text": "answer must be correct"}],
            "expectedResponse": {"text": json.dumps(payload)},
            "expectedTrajectory": {"toolNames": ["execute_sql_query"]},
        }
    ]
    gold = handler._extract_gold(refs)
    assert gold["gold_sql"] == "SELECT 1"
    assert gold["db_id"] == "bird_cs"
    assert gold["catalog"] == "s3tablescatalog/b"


def test_extract_gold_from_assertions_fallback(handler):
    """When expectedResponse is absent, a JSON assertion carrying gold_sql works."""
    payload = {"gold_sql": "SELECT 2", "db_id": "bird_cs"}
    refs = [{"assertions": [{"text": json.dumps(payload)}]}]
    gold = handler._extract_gold(refs)
    assert gold["gold_sql"] == "SELECT 2"


def test_extract_gold_missing_raises(handler):
    """No gold payload anywhere → fail loudly."""
    with pytest.raises(ValueError):
        handler._extract_gold([{"assertions": [{"text": "no json here"}]}])


# ── handler end-to-end (fake Athena, monkeypatched run_sql) ──────────────────


def _make_event(evaluator_name: str, predicted_sql: str, gold_sql: str = "SELECT 1"):
    """Build a minimal AgentCore codeBased-evaluator event."""
    gold_payload = {
        "gold_sql": gold_sql,
        "db_id": "bird_cs",
        "catalog": "s3tablescatalog/b",
    }
    return {
        "schemaVersion": "1.0",
        "evaluatorName": evaluator_name,
        "evaluationLevel": "TRACE",
        "evaluationInput": {
            "sessionSpans": [
                {
                    "attributes": {
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": "execute_sql_query",
                        "gen_ai.tool.input": json.dumps({"sql": predicted_sql}),
                    }
                }
            ]
            if predicted_sql
            else []
        },
        "evaluationReferenceInputs": [
            {"expectedResponse": {"text": json.dumps(gold_payload)}}
        ],
        "evaluationTarget": {"traceIds": ["t1"]},
    }


def _patch_run_sql(
    handler, monkeypatch, *, gold_rows, pred_rows, gold_ms=100, pred_ms=100
):
    """Monkeypatch the handler's run_sql to return canned results by SQL text."""
    from athena_bench import AthenaResult  # vendored copy on the handler dir path

    def fake_run_sql(
        *, athena, sql, workgroup, output_loc, database_name="", catalog_id="", **kw
    ):
        if sql == "SELECT 1":  # the gold SQL in _make_event
            return AthenaResult(columns=["c"], rows=gold_rows, elapsed_ms=gold_ms)
        return AthenaResult(columns=["c"], rows=pred_rows, elapsed_ms=pred_ms)

    monkeypatch.setattr(handler, "run_sql", fake_run_sql)
    monkeypatch.setattr(
        handler,
        "boto3",
        type("B", (), {"client": staticmethod(lambda *a, **k: object())}),
    )


def test_handler_ex_pass(handler, monkeypatch):
    """Matching result sets → EX value 1.0, label PASS."""
    _patch_run_sql(handler, monkeypatch, gold_rows=[("5",)], pred_rows=[("5",)])
    event = _make_event(
        "bird_ExecutionAccuracy", predicted_sql="SELECT count(*) FROM t"
    )
    out = handler.handler(event, None)
    assert out["value"] == 1.0
    assert out["label"] == "PASS"


def test_handler_ex_fail_on_wrong_pred(handler, monkeypatch):
    """Mismatched result sets → EX value 0.0, label FAIL."""
    _patch_run_sql(handler, monkeypatch, gold_rows=[("5",)], pred_rows=[("9",)])
    event = _make_event(
        "bird_ExecutionAccuracy", predicted_sql="SELECT count(*) FROM t"
    )
    out = handler.handler(event, None)
    assert out["value"] == 0.0
    assert out["label"] == "FAIL"


def test_handler_miss_explanation_includes_row_values(handler, monkeypatch):
    """A MISS must record the first pred/gold row so the mismatch is diagnosable.

    Row counts alone cannot distinguish an extra/reordered COLUMN (EX is a strict
    set-match, sensitive to column count and order) from a genuine value
    disagreement — the two look identical in the results JSON. This asserts the
    values are present on a same-cardinality miss, the exact case that previously
    required replaying queries through Athena history by hand.
    """
    _patch_run_sql(
        handler,
        monkeypatch,
        gold_rows=[("185",)],
        pred_rows=[("Amber", "185")],  # over-projection: right value, extra column
    )
    event = _make_event(
        "bird_ExecutionAccuracy", predicted_sql="SELECT name, height FROM t"
    )
    out = handler.handler(event, None)
    assert out["value"] == 0.0
    assert "pred_row0=" in out["explanation"]
    assert "gold_row0=" in out["explanation"]
    assert "Amber" in out["explanation"]


def test_handler_pass_explanation_omits_row_values(handler, monkeypatch):
    """A PASS needs no row dump — values are only attached on a miss."""
    _patch_run_sql(handler, monkeypatch, gold_rows=[("5",)], pred_rows=[("5",)])
    event = _make_event(
        "bird_ExecutionAccuracy", predicted_sql="SELECT count(*) FROM t"
    )
    out = handler.handler(event, None)
    assert out["value"] == 1.0
    assert "pred_row0=" not in out["explanation"]


def test_preview_row_handles_empty_and_truncates(handler):
    """Empty rows render a sentinel; a wide row is capped so the payload stays small."""
    assert handler._preview_row([]) == "<no rows>"
    wide = handler._preview_row([("x" * 500,)], max_chars=50)
    assert len(wide) <= 51 and wide.endswith("…")


def test_handler_empty_spans_is_unmeasurable_not_zero(handler, monkeypatch):
    """EMPTY sessionSpans must return TelemetryUnavailable, never a score of 0.

    These metrics are span-derived, so an empty span list means the trace had not
    landed in CloudWatch when the evaluator ran — the agent may have emitted
    perfect SQL. Scoring 0.0 folds telemetry loss into the accuracy number and is
    indistinguishable from a wrong answer; an errorCode drops the row from the
    denominator instead. This regression-pins the fix for the 142/500 rows on the
    2026-08-09 BIRD run that scored 0.0 with no input at all.
    """
    _patch_run_sql(handler, monkeypatch, gold_rows=[("5",)], pred_rows=[])
    event = _make_event("bird_ExecutionAccuracy", predicted_sql="")
    assert event["evaluationInput"]["sessionSpans"] == []  # guard the fixture
    out = handler.handler(event, None)
    assert out["errorCode"] == "TelemetryUnavailable"
    assert "value" not in out and "label" not in out


def test_handler_spans_without_sql_scores_zero(handler, monkeypatch):
    """Spans PRESENT but carrying no SQL is a genuine agent failure → score 0.

    This is the other side of the TelemetryUnavailable split: we observed the
    trace, and it contained no query. That must stay in the denominator.
    """
    _patch_run_sql(handler, monkeypatch, gold_rows=[("5",)], pred_rows=[])
    event = _make_event("bird_ExecutionAccuracy", predicted_sql="")
    # A real, non-empty trace that simply never executed SQL.
    event["evaluationInput"]["sessionSpans"] = [
        {"name": "invoke_agent", "attributes": {"gen_ai.operation.name": "invoke_agent"}}
    ]
    out = handler.handler(event, None)
    assert out["value"] == 0.0
    assert out["label"] == "FAIL"
    assert "errorCode" not in out


def test_handler_gold_failure_is_evaluator_error(handler, monkeypatch):
    """A gold-SQL execution failure returns an errorCode (evaluator error)."""
    from athena_bench import AthenaResult  # noqa: F401 — ensure vendored path importable

    def boom_run_sql(
        *, athena, sql, workgroup, output_loc, database_name="", catalog_id="", **kw
    ):
        raise RuntimeError("Athena FAILED: table not found")

    monkeypatch.setattr(handler, "run_sql", boom_run_sql)
    monkeypatch.setattr(
        handler,
        "boto3",
        type("B", (), {"client": staticmethod(lambda *a, **k: object())}),
    )
    event = _make_event("bird_ExecutionAccuracy", predicted_sql="SELECT 1")
    out = handler.handler(event, None)
    assert "errorCode" in out
    assert "errorMessage" in out


def test_handler_pred_failure_scores_zero(handler, monkeypatch):
    """GOLD succeeds but the PREDICTED query raises → value 0.0, label, NO errorCode.

    A bad predicted query is a score (the model wrote un-runnable SQL), not an
    evaluator fault — so the pred-execution-failure branch must return a normal
    {label, value, explanation}, never an errorCode.
    """
    from athena_bench import AthenaResult  # vendored copy on the handler dir path

    def fake_run_sql(
        *, athena, sql, workgroup, output_loc, database_name="", catalog_id="", **kw
    ):
        if sql == "SELECT 1":  # the gold SQL in _make_event — succeeds
            return AthenaResult(columns=["c"], rows=[("5",)], elapsed_ms=50)
        raise RuntimeError("Athena FAILED: syntax error in predicted SQL")

    monkeypatch.setattr(handler, "run_sql", fake_run_sql)
    monkeypatch.setattr(
        handler,
        "boto3",
        type("B", (), {"client": staticmethod(lambda *a, **k: object())}),
    )
    event = _make_event("bird_ExecutionAccuracy", predicted_sql="SELECT bogus(")
    out = handler.handler(event, None)
    assert out["value"] == 0.0
    assert out["label"] == "FAIL"
    assert "explanation" in out
    assert "errorCode" not in out


def test_handler_rves_pass_on_match(handler, monkeypatch):
    """evaluatorName → rves: a correct result set scores > 0, label PASS."""
    # gold_ms > pred_ms → prediction faster → reward band above 0.
    _patch_run_sql(
        handler,
        monkeypatch,
        gold_rows=[("5",)],
        pred_rows=[("5",)],
        gold_ms=200,
        pred_ms=100,
    )
    event = _make_event("bird_RewardVES", predicted_sql="SELECT count(*) FROM t")
    out = handler.handler(event, None)
    assert out["value"] > 0.0
    assert out["label"] == "PASS"


def test_handler_soft_f1_pass_on_match(handler, monkeypatch):
    """evaluatorName → soft_f1: an exact result set match scores 1.0, label PASS."""
    _patch_run_sql(handler, monkeypatch, gold_rows=[("5",)], pred_rows=[("5",)])
    event = _make_event("bird_SoftF1", predicted_sql="SELECT count(*) FROM t")
    out = handler.handler(event, None)
    assert out["value"] == 1.0
    assert out["label"] == "PASS"


def test_handler_unknown_evaluator_is_error(handler):
    """An unknown evaluator name (and no EVAL_METRIC env) returns an errorCode."""
    event = _make_event("MysteryMetric", predicted_sql="SELECT 1")
    out = handler.handler(event, None)
    assert "errorCode" in out


# ── EVAL_METRIC env-var path (the deployed-function discriminator) ────────────


def test_handler_env_metric_selects_ex(handler, monkeypatch):
    """EVAL_METRIC=ex picks the EX metric even when the event has NO evaluatorName
    (the real AgentCore batch-path shape)."""
    monkeypatch.setenv("EVAL_METRIC", "ex")
    _patch_run_sql(handler, monkeypatch, gold_rows=[("5",)], pred_rows=[("5",)])
    event = _make_event("bird_SoftF1", predicted_sql="SELECT count(*) FROM t")
    # Drop evaluatorName to mimic the batch path — the env var must still resolve.
    event.pop("evaluatorName", None)
    out = handler.handler(event, None)
    assert out["value"] == 1.0 and out["label"] == "PASS"


def test_handler_env_metric_overrides_name(handler, monkeypatch):
    """EVAL_METRIC takes precedence over a (possibly stale) evaluatorName."""
    monkeypatch.setenv("EVAL_METRIC", "soft_f1")
    _patch_run_sql(handler, monkeypatch, gold_rows=[("5",)], pred_rows=[("5",)])
    # Name says EX, env says soft_f1 → soft_f1 wins (both PASS here, so assert the
    # env path is taken by using a partial-overlap case where EX and Soft-F1 differ).
    _patch_run_sql(
        handler, monkeypatch, gold_rows=[("5",), ("6",)], pred_rows=[("5",)]
    )
    event = _make_event("bird_ExecutionAccuracy", predicted_sql="SELECT x FROM t")
    out = handler.handler(event, None)
    # EX would be 0 (result sets differ); Soft-F1 is >0 (partial overlap). A >0
    # value proves the env-selected metric (soft_f1) ran, not EX.
    assert out["value"] > 0.0


def test_handler_bad_env_metric_is_error(handler, monkeypatch):
    """An invalid EVAL_METRIC value fails loud with an errorCode."""
    monkeypatch.setenv("EVAL_METRIC", "bogus")
    event = _make_event("bird_ExecutionAccuracy", predicted_sql="SELECT 1")
    out = handler.handler(event, None)
    assert out.get("errorCode") == "BadEvalMetricEnv"
