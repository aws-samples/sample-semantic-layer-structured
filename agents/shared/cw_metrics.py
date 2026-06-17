"""Lightweight CloudWatch EMF metrics emitter.

EMF (embedded metric format) lets us avoid a synchronous ``PutMetricData``
call on the hot path; CloudWatch parses the metric values out of the JSON
log line emitted to stdout.
"""
from __future__ import annotations

import json
import time
from typing import Dict, Optional

NAMESPACE = "SemanticLayer/Query"


def emit(metric_name: str, value: float = 1.0, *,
         dimensions: Optional[Dict[str, str]] = None) -> None:
    """Print one EMF log line for ``metric_name``.

    Args:
        metric_name: Metric name registered under ``NAMESPACE``.
        value: Numeric value to record (defaults to 1 for counters).
        dimensions: Optional ``{name: value}`` map written both as a
            CloudWatch dimension list and as top-level fields.
    """
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [list((dimensions or {}).keys())],
                "Metrics": [{"Name": metric_name, "Unit": "Count"}],
            }],
        },
        metric_name: value,
        **(dimensions or {}),
    }
    print(json.dumps(payload), flush=True)
