"""Monitoring service — production-signal breakdown per semantic layer.

Backs the admin "Monitoring" tab. It answers two questions about LIVE query
traffic for one semantic layer. "Live traffic" spans every transport that reaches
the agents' chat path and persists a chat-sessions row — AG-UI chat, MCP tool
calls, and eval-notebook runs — each tagged by a session-level ``source`` field
(``chat`` / ``mcp`` / ``eval``). (Direct request/response invokes that bypass the
chat path are not persisted and so are not counted; see the transport-agnostic
monitoring plan.) The two questions:

  1. HOW did queries resolve? Every query-agent response carries a
     ``provenance`` object (``agents/shared/provenance.py``) persisted on the
     assistant turn at ``messages[].totals.provenance``. We bucket each answered
     turn by ``provenance.tier`` into the four resolution layers:

        metric layer    <- tier "governed_metric"  (Tier 1 governed metric)
        semantic layer  <- tier "semantic_sql" | "vkg"  (Tier 2 graph: slice->SQL)
        advisory layer  <- tier "advisory"  (schema / "what can I ask" questions)
        agentic layer   <- NOT IMPLEMENTED yet (always 0; surfaced so the tab
                           documents the planned bucket rather than hiding it)

  2. HOW OFTEN do users CORRECT the agent? We run the correction-language
     heuristic (``correction_language.is_correction``) over each persisted USER
     turn and report the share that read as a correction ("that's the wrong
     table", "you're missing the fraud filter"). This is correlated with the
     count of long-term lessons AgentCore Memory has extracted for the layer
     (each correction is a candidate mapping-lesson), so the operator can see
     whether corrections are being captured as durable lessons.

DATA SOURCE & SCOPING. The ``chat-sessions`` DDB table is keyed by
``sessionId`` and has no GSI on ``ontologyId`` and stores no layer VERSION, so
this aggregation Scans the table with a ``FilterExpression`` on ``ontologyId``.
That is acceptable for a low-frequency admin view over a TTL-bounded table (rows
expire 24h after last activity, so the scan stays small), and we cap the pages
scanned. Because turns carry no per-turn version, the breakdown is scoped to the
LAYER (all versions); the lessons count, whose memory namespace DOES carry
version, is reported layer-wide to match. This is READ-ONLY and needs no
agent-runtime change.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr

from services.agentcore_memory_service import AgentCoreMemoryService
from services.correction_language import is_correction, matched_phrases

logger = logging.getLogger(__name__)

# provenance.tier -> resolution-layer bucket key. Two Tier-2 tiers (plain
# semantic SQL and the VKG/Ontop path) both fold into "semantic".
_TIER_TO_BUCKET: Dict[str, str] = {
    "governed_metric": "metric",
    "semantic_sql": "semantic",
    "vkg": "semantic",
    "advisory": "advisory",
}

# The agentic layer is not implemented; it is always reported as 0 so the tab
# documents the planned bucket explicitly rather than silently omitting it.
_AGENTIC_BUCKET = "agentic"

# All resolution buckets, in display order. "agentic" is included so the tab
# always renders five buckets even though it is currently always zero.
_ALL_BUCKETS: List[str] = ["metric", "semantic", "advisory", _AGENTIC_BUCKET]

# Bound the scan so a pathological table size can't run the admin request to the
# Lambda timeout. The chat-sessions table is TTL-bounded (24h), so in practice
# this is far more than a layer accrues in a day.
_MAX_SCAN_PAGES: int = 40

# Cap the number of concrete correction examples returned to the UI.
_MAX_CORRECTION_EXAMPLES: int = 10


class MonitoringService:
    """Aggregate per-layer resolution + correction signals from chat sessions."""

    def __init__(
        self,
        *,
        table_name: Optional[str] = None,
        region: Optional[str] = None,
        ddb_resource: Any = None,
        memory_service: Optional[AgentCoreMemoryService] = None,
    ) -> None:
        """Bind to the chat-sessions table and the lessons memory service.

        Args:
            table_name: Override the chat-sessions table name (defaults to env
                ``CHAT_SESSIONS_TABLE``).
            region: AWS region (defaults to env ``AWS_REGION``, then us-east-1).
            ddb_resource: Pre-built ``boto3.resource('dynamodb')`` (test seam).
            memory_service: Pre-built ``AgentCoreMemoryService`` (test seam);
                used to correlate corrections with extracted lessons.
        """
        self._table_name = table_name or os.environ.get("CHAT_SESSIONS_TABLE", "")
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._ddb_resource = ddb_resource
        self._table = None
        self._memory_service = memory_service or AgentCoreMemoryService()

        if not self._table_name:
            logger.warning(
                "CHAT_SESSIONS_TABLE is empty — monitoring breakdown will be empty"
            )

    @property
    def configured(self) -> bool:
        """True when a chat-sessions table is wired; otherwise the tab is empty."""
        return bool(self._table_name)

    def _get_table(self):
        """Return a cached boto3 Table handle for the chat-sessions table."""
        if self._table is None:
            resource = self._ddb_resource or boto3.resource(
                "dynamodb", region_name=self._region
            )
            self._table = resource.Table(self._table_name)
        return self._table

    def _scan_sessions(self, *, ontology_id: str) -> List[Dict[str, Any]]:
        """Scan the chat-sessions table for one layer's sessions.

        Uses a ``FilterExpression`` on ``ontologyId`` (no GSI exists). Pages are
        bounded by ``_MAX_SCAN_PAGES``; if the cap is hit we log it so a
        truncated aggregate is never mistaken for a complete one.

        Args:
            ontology_id: The semantic-layer id to scope to.

        Returns:
            The matching session items (each carries the ``messages`` list).
        """
        table = self._get_table()
        items: List[Dict[str, Any]] = []
        scan_kwargs: Dict[str, Any] = {
            "FilterExpression": Attr("ontologyId").eq(ontology_id),
        }
        pages = 0
        while True:
            resp = table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            pages += 1
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            if pages >= _MAX_SCAN_PAGES:
                logger.warning(
                    "monitoring scan for %s hit the %d-page cap — breakdown is "
                    "truncated (table larger than expected for a TTL-bounded store)",
                    ontology_id, _MAX_SCAN_PAGES,
                )
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
        return items

    @staticmethod
    def _bucket_for_message(message: Dict[str, Any]) -> Optional[str]:
        """Return the resolution bucket for one assistant turn, or None to skip.

        Reads ``totals.provenance.tier`` and maps it through ``_TIER_TO_BUCKET``.
        Turns without a provenance tier (e.g. a pure clarification turn that
        emitted no answer, or a turn persisted before provenance existed) return
        None and are NOT counted — counting them would dilute every bucket.

        Args:
            message: One persisted message record.

        Returns:
            A bucket key from ``_ALL_BUCKETS``, or None when the turn carries no
            recognizable resolution tier.
        """
        if message.get("role") != "assistant":
            return None
        totals = message.get("totals")
        if not isinstance(totals, dict):
            return None
        provenance = totals.get("provenance")
        if not isinstance(provenance, dict):
            return None
        tier = provenance.get("tier")
        return _TIER_TO_BUCKET.get(tier)

    def aggregate(self, *, ontology_id: str) -> Dict[str, Any]:
        """Aggregate the resolution + correction breakdown for one layer.

        Args:
            ontology_id: The semantic-layer id to report on.

        Returns:
            A JSON-serializable dict:
              {
                "ontologyId": str,
                "configured": bool,            # false when the table isn't wired
                "sessionCount": int,
                "resolution": {
                  "totalAnswered": int,        # turns with a recognized tier
                  "buckets": [                 # one per _ALL_BUCKETS, display order
                    {"key","label","count","pct","implemented"}
                  ],
                },
                "corrections": {
                  "userTurns": int,
                  "correctionTurns": int,
                  "pct": float,
                  "examples": [str, ...],      # up to _MAX_CORRECTION_EXAMPLES
                  "lessonsExtracted": int,     # long-term records in memory
                  "lessonsCapped": bool,       # True when the count hit the 100 ceiling
                },
              }
        """
        lessons_count, lessons_capped = self._lessons_count(ontology_id=ontology_id)
        result_base: Dict[str, Any] = {
            "ontologyId": ontology_id,
            "configured": self.configured,
            "sessionCount": 0,
            "resolution": {
                "totalAnswered": 0,
                "buckets": self._empty_buckets(),
            },
            "corrections": {
                "userTurns": 0,
                "correctionTurns": 0,
                "pct": 0.0,
                "examples": [],
                "lessonsExtracted": lessons_count,
                "lessonsCapped": lessons_capped,
            },
        }
        if not self.configured:
            return result_base

        sessions = self._scan_sessions(ontology_id=ontology_id)
        result_base["sessionCount"] = len(sessions)

        bucket_counts: Dict[str, int] = {b: 0 for b in _ALL_BUCKETS}
        user_turns = 0
        correction_turns = 0
        examples: List[str] = []

        for session in sessions:
            for message in session.get("messages") or []:
                role = message.get("role")
                if role == "assistant":
                    bucket = self._bucket_for_message(message)
                    if bucket is not None:
                        bucket_counts[bucket] += 1
                elif role == "user":
                    user_turns += 1
                    text = message.get("text") or ""
                    if is_correction(text):
                        correction_turns += 1
                        if len(examples) < _MAX_CORRECTION_EXAMPLES:
                            examples.extend(
                                p for p in matched_phrases(text)
                                if len(examples) < _MAX_CORRECTION_EXAMPLES
                            )

        total_answered = sum(bucket_counts.values())
        result_base["resolution"] = {
            "totalAnswered": total_answered,
            "buckets": self._build_buckets(
                bucket_counts=bucket_counts, total=total_answered
            ),
        }
        result_base["corrections"].update(
            {
                "userTurns": user_turns,
                "correctionTurns": correction_turns,
                "pct": self._pct(correction_turns, user_turns),
                "examples": examples,
            }
        )
        return result_base

    def _lessons_count(self, *, ontology_id: str) -> tuple[int, bool]:
        """Return (lesson_count, capped) for this layer's long-term lessons.

        AgentCore's ``ListMemoryRecords`` hard-caps a page at 100 and
        ``AgentCoreMemoryService.list_records`` does not paginate, so a layer
        with more than 100 lessons reports exactly 100. We return a ``capped``
        flag (True when the page is full) so the UI can label the figure
        honestly ("100+") rather than presenting a clamped value as exact.

        Best-effort — when the memory resource isn't configured (or the call
        fails) ``list_records`` returns ``[]`` and we report ``(0, False)``
        rather than failing the whole monitoring request.

        Returns:
            ``(count, capped)`` where ``capped`` is True iff the count reached
            the 100-record ceiling (i.e. the true total may be higher).
        """
        records = self._memory_service.list_records(
            ontology_id=ontology_id, max_results=100
        )
        return len(records), len(records) >= 100

    @staticmethod
    def _pct(part: int, whole: int) -> float:
        """Percentage ``part/whole`` rounded to 1 dp; 0.0 when whole is 0."""
        return round(100.0 * part / whole, 1) if whole else 0.0

    @staticmethod
    def _empty_buckets() -> List[Dict[str, Any]]:
        """Zeroed bucket list (used for the not-configured / empty path)."""
        return [
            {
                "key": key,
                "label": _BUCKET_LABELS[key],
                "count": 0,
                "pct": 0.0,
                "implemented": key != _AGENTIC_BUCKET,
            }
            for key in _ALL_BUCKETS
        ]

    def _build_buckets(
        self, *, bucket_counts: Dict[str, int], total: int
    ) -> List[Dict[str, Any]]:
        """Build the per-bucket count+percentage list in display order."""
        return [
            {
                "key": key,
                "label": _BUCKET_LABELS[key],
                "count": bucket_counts.get(key, 0),
                "pct": self._pct(bucket_counts.get(key, 0), total),
                # The agentic layer is not implemented yet; the UI greys it out.
                "implemented": key != _AGENTIC_BUCKET,
            }
            for key in _ALL_BUCKETS
        ]


# Human-readable bucket labels for the tab.
_BUCKET_LABELS: Dict[str, str] = {
    "metric": "Metric layer",
    "semantic": "Semantic layer",
    "advisory": "Advisory layer",
    _AGENTIC_BUCKET: "Agentic layer",
}
