"""Unit tests for token-usage accounting (agents/shared/tier2_graph.py).

Regression for the "numbers don't add up" report: Bedrock Converse with prompt
caching reports cacheRead/cacheWrite tokens SEPARATELY from inputTokens, while
totalTokens is the cache-inclusive grand total. The footer showed
"61,427 tokens (10,108 in / 1,887 out)" because the ~49k cache-read portion was
folded into the total but dropped from the in/out breakdown. extract_usage /
add_usage must capture the cache fields so the components reconcile with the total.
"""
from agents.shared.tier2_graph import WorkflowContext, add_usage, extract_usage


class _FakeMetrics:
    def __init__(self, usage):
        self.accumulated_usage = usage


class _FakeResult:
    def __init__(self, usage):
        self.metrics = _FakeMetrics(usage)


def test_extract_usage_captures_cache_fields():
    # A Bedrock-shaped cache-inclusive usage dict: total = in + out + cacheRead.
    result = _FakeResult({
        "inputTokens": 10108,
        "outputTokens": 1887,
        "totalTokens": 61427,
        "cacheReadInputTokens": 49432,
    })
    usage = extract_usage(result)
    assert usage["inputTokens"] == 10108
    assert usage["outputTokens"] == 1887
    assert usage["totalTokens"] == 61427
    assert usage["cacheReadInputTokens"] == 49432
    assert usage["cacheWriteInputTokens"] == 0
    # The components now reconcile with the total (the bug: they didn't, because
    # cacheRead was invisible).
    assert (
        usage["inputTokens"]
        + usage["outputTokens"]
        + usage["cacheReadInputTokens"]
        + usage["cacheWriteInputTokens"]
        == usage["totalTokens"]
    )


def test_extract_usage_zeros_when_metrics_absent():
    class _NoMetrics:
        pass

    usage = extract_usage(_NoMetrics())
    assert usage == {
        "inputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 0,
    }


def test_add_usage_accumulates_cache_fields():
    ctx = WorkflowContext(question="q", namespace="ns")
    add_usage(ctx, {
        "inputTokens": 100, "outputTokens": 20, "totalTokens": 620,
        "cacheReadInputTokens": 500,
    })
    add_usage(ctx, {
        "inputTokens": 10, "outputTokens": 5, "totalTokens": 215,
        "cacheReadInputTokens": 200,
    })
    assert ctx.usage["inputTokens"] == 110
    assert ctx.usage["outputTokens"] == 25
    assert ctx.usage["cacheReadInputTokens"] == 700
    assert ctx.usage["totalTokens"] == 835
    # Running total reconciles with the accumulated components.
    assert (
        ctx.usage["inputTokens"]
        + ctx.usage["outputTokens"]
        + ctx.usage["cacheReadInputTokens"]
        + ctx.usage.get("cacheWriteInputTokens", 0)
        == ctx.usage["totalTokens"]
    )
