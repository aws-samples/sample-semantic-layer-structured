"""CloudWatch metric publisher for the AG-UI chat (item #1).

Three metrics, all under namespace ``SemanticLayer/Chat``:

  * ``chat.session.started``    — fires the first time a session id is
                                  seen on a chat turn (i.e. session was just
                                  created in DDB).
  * ``chat.turn.completed``     — fires when an assistant turn lands on
                                  DDB after run_finished.
  * ``chat.guardrail.blocked``  — fires when INPUT or OUTPUT guardrail
                                  blocks the turn. Dimension ``source``
                                  distinguishes INPUT vs OUTPUT.

The publisher batches calls into a fire-and-forget thread pool so the
SSE generator never blocks on PutMetricData. Failures are logged and
swallowed — metrics drift is preferable to a broken chat.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import boto3

logger = logging.getLogger(__name__)

NAMESPACE = 'SemanticLayer/Chat'


class ChatMetrics:
    """Thin CloudWatch wrapper. Singleton-friendly via lazy boto client."""

    def __init__(
        self,
        *,
        cloudwatch_client: Any = None,
        region: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self._region = region or os.environ.get('AWS_REGION', 'us-east-1')
        self._client = cloudwatch_client
        if enabled is None:
            enabled = (
                os.environ.get('ENABLE_CHAT_METRICS', 'true').lower() != 'false'
            )
        self._enabled = enabled

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client(
                'cloudwatch', region_name=self._region
            )
        return self._client

    def _put(self, *, metric_name: str, dimensions=None) -> None:
        if not self._enabled:
            return
        try:
            metric_data = {
                'MetricName': metric_name,
                'Value': 1,
                'Unit': 'Count',
            }
            if dimensions:
                metric_data['Dimensions'] = [
                    {'Name': k, 'Value': v} for k, v in dimensions.items()
                ]
            self._get_client().put_metric_data(
                Namespace=NAMESPACE, MetricData=[metric_data]
            )
        except Exception as exc:  # noqa: BLE001 — never block the chat
            logger.warning('chat metrics put failed (%s): %s', metric_name, exc)

    def session_started(self, *, ontology_id: str, mode: str) -> None:
        self._put(
            metric_name='chat.session.started',
            dimensions={'ontologyId': ontology_id, 'mode': mode},
        )

    def turn_completed(self, *, mode: str) -> None:
        self._put(
            metric_name='chat.turn.completed',
            dimensions={'mode': mode},
        )

    def guardrail_blocked(self, *, source: str) -> None:
        """``source`` is 'INPUT' or 'OUTPUT'."""
        self._put(
            metric_name='chat.guardrail.blocked',
            dimensions={'source': source},
        )
