"""Unit tests for the EMF CloudWatch-metrics emitter."""
import json

from agents.shared import cw_metrics


def test_emit_writes_emf_log_line(capsys):
    cw_metrics.emit("query.tier1.hits", dimensions={"namespace": "ns"})
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["query.tier1.hits"] == 1.0
    assert payload["namespace"] == "ns"
    assert (
        payload["_aws"]["CloudWatchMetrics"][0]["Namespace"]
        == "SemanticLayer/Query"
    )
