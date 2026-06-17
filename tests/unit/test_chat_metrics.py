"""Unit tests for ChatMetrics."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)

from services.chat_metrics import ChatMetrics, NAMESPACE  # noqa: E402


def test_session_started_emits_metric():
    cw = MagicMock()
    metrics = ChatMetrics(cloudwatch_client=cw, enabled=True)
    metrics.session_started(ontology_id='ont-1', mode='vkg')
    cw.put_metric_data.assert_called_once()
    args = cw.put_metric_data.call_args.kwargs
    assert args['Namespace'] == NAMESPACE
    md = args['MetricData'][0]
    assert md['MetricName'] == 'chat.session.started'
    assert md['Value'] == 1
    assert md['Unit'] == 'Count'
    dims = {d['Name']: d['Value'] for d in md['Dimensions']}
    assert dims == {'ontologyId': 'ont-1', 'mode': 'vkg'}


def test_turn_completed_emits_metric():
    cw = MagicMock()
    metrics = ChatMetrics(cloudwatch_client=cw, enabled=True)
    metrics.turn_completed(mode='semantic-rag')
    md = cw.put_metric_data.call_args.kwargs['MetricData'][0]
    assert md['MetricName'] == 'chat.turn.completed'
    dims = {d['Name']: d['Value'] for d in md['Dimensions']}
    assert dims == {'mode': 'semantic-rag'}


def test_guardrail_blocked_carries_source_dimension():
    cw = MagicMock()
    metrics = ChatMetrics(cloudwatch_client=cw, enabled=True)
    metrics.guardrail_blocked(source='INPUT')
    md = cw.put_metric_data.call_args.kwargs['MetricData'][0]
    dims = {d['Name']: d['Value'] for d in md['Dimensions']}
    assert dims == {'source': 'INPUT'}


def test_disabled_metrics_skip_cloudwatch():
    cw = MagicMock()
    metrics = ChatMetrics(cloudwatch_client=cw, enabled=False)
    metrics.session_started(ontology_id='o', mode='vkg')
    metrics.turn_completed(mode='vkg')
    metrics.guardrail_blocked(source='INPUT')
    cw.put_metric_data.assert_not_called()


def test_cloudwatch_failure_is_swallowed():
    cw = MagicMock()
    cw.put_metric_data.side_effect = RuntimeError('CW down')
    metrics = ChatMetrics(cloudwatch_client=cw, enabled=True)
    # Must not raise.
    metrics.session_started(ontology_id='o', mode='vkg')
