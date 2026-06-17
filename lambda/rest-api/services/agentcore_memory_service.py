"""Thin wrapper around the bedrock-agentcore data-plane Memory APIs.

Used by the rewritten ``lessons_api`` to:
  - list long-term semantic-strategy records for one ontology (``list_memory_records``)
  - delete a record by ID (``delete_memory_record``)

There is no write surface here — agents persist turns through the Strands
``LessonsMemoryHooks`` provider, which already PII-redacts via Bedrock
Guardrails.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import boto3

logger = logging.getLogger(__name__)

# AgentCore's ``ListMemoryRecords`` rejects ``maxResults`` > 100 with a
# ``ValidationException``. Clamp to this ceiling so an over-large caller limit
# degrades to a full page instead of throwing (which the broad except below
# would otherwise swallow into an empty list — see the maxResults bug where the
# admin UI requested 200 and silently showed "no lessons").
_AGENTCORE_MAX_RESULTS = 100


class AgentCoreMemoryService:
    """Read-and-delete client for AgentCore Memory long-term records."""

    def __init__(
        self,
        *,
        memory_id: Optional[str] = None,
        region: Optional[str] = None,
        client: Any = None,
    ) -> None:
        """Bind to a memory resource.

        Args:
            memory_id: The AgentCore Memory id; defaults to env ``LESSONS_MEMORY_ID``.
            region: AWS region; defaults to env ``AWS_REGION`` (or us-east-1).
            client: Pre-built boto3 ``bedrock-agentcore`` client (test seam).
        """
        self._memory_id = memory_id or os.environ.get('LESSONS_MEMORY_ID', '')
        self._region = region or os.environ.get('AWS_REGION', 'us-east-1')
        self._client = client

        if not self._memory_id:
            logger.warning(
                "LESSONS_MEMORY_ID is empty — lessons endpoints will return []"
            )

    @property
    def configured(self) -> bool:
        """True when a memory id is set; otherwise endpoints short-circuit."""
        return bool(self._memory_id)

    def _get_client(self):
        if self._client is None:
            # bedrock-agentcore (data-plane) is the runtime client; the
            # control-plane is bedrock-agentcore-control. list/delete memory
            # records are data-plane operations.
            self._client = boto3.client(
                'bedrock-agentcore', region_name=self._region
            )
        return self._client

    def list_records(
        self,
        *,
        ontology_id: str,
        max_results: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return long-term records for one layer across all versions/users/sessions.

        With the namespace template ``/lessons/{actorId}/{sessionId}/`` and
        ``actorId = "<semanticLayerId>/<semanticLayerVersion>/<userId>"``, the
        resolved per-record namespace is
        ``/lessons/<semanticLayerId>/<semanticLayerVersion>/<userId>/<sessionId>/``.
        The admin UI surfaces lessons per layer, so we list using the
        layer-level prefix ``/lessons/<semanticLayerId>/`` — AgentCore's
        ``namespace`` filter on ``list_memory_records`` matches by prefix, so this
        spans every version, user, and session for the layer.

        Args:
            ontology_id: The semantic-layer id to scope to (the first namespace
                segment; matches every version/user/session beneath it).
            max_results: Desired cap; clamped to AgentCore's hard ceiling of
                100 (``_AGENTCORE_MAX_RESULTS``) before the call.

        Returns:
            A list of plain-dict records, each with keys: ``memoryRecordId``,
            ``content`` (string), ``namespaces`` (list[str]), ``createdAt``
            (ISO timestamp). Returns an empty list when the memory resource
            isn't configured.
        """
        if not self.configured:
            return []

        # AgentCore caps maxResults at 100; clamp so a larger caller limit
        # returns a page rather than tripping a ValidationException.
        page_size = min(max_results, _AGENTCORE_MAX_RESULTS)
        namespace = f"/lessons/{ontology_id}/"
        try:
            response = self._get_client().list_memory_records(
                memoryId=self._memory_id,
                namespace=namespace,
                maxResults=page_size,
            )
        except Exception as exc:  # noqa: BLE001 — surface to caller as empty list
            logger.error(
                "list_memory_records failed for namespace=%s: %s",
                namespace, exc,
            )
            return []

        out: List[Dict[str, Any]] = []
        for raw in response.get('memoryRecordSummaries', []) or []:
            content = raw.get('content') or {}
            text = content.get('text') if isinstance(content, dict) else ''
            out.append(
                {
                    'memoryRecordId': raw.get('memoryRecordId', ''),
                    'content': text or '',
                    'namespaces': raw.get('namespaces', []) or [],
                    'createdAt': str(raw.get('createdAt', '')),
                }
            )
        return out

    @staticmethod
    def _actor_session_from_namespace(namespace: str) -> Optional[tuple]:
        """Parse ``actorId`` and ``sessionId`` out of a lessons namespace.

        The memory resource's strategy template
        ``/lessons/{actorId}/{sessionId}/`` resolves (per
        ``agents/shared/memory_hooks.py``) to
        ``/lessons/<layerId>/<layerVersion>/<userId>/<sessionId>/`` where
        ``actorId == "<layerId>/<layerVersion>/<userId>"``. So the trailing
        path segment is the ``sessionId`` and everything between ``/lessons/``
        and that segment is the ``actorId`` (which itself contains slashes).

        Returns:
            ``(actor_id, session_id)`` or ``None`` when the namespace doesn't
            match the expected ``/lessons/<…>/<session>/`` shape.
        """
        if not namespace:
            return None
        # Strip the leading "/lessons/" prefix and any surrounding slashes.
        trimmed = namespace.strip("/")
        parts = trimmed.split("/")
        # Expect at least: "lessons", actorId(≥1 segment), sessionId.
        if len(parts) < 3 or parts[0] != "lessons":
            return None
        session_id = parts[-1]
        actor_id = "/".join(parts[1:-1])
        if not actor_id or not session_id:
            return None
        return actor_id, session_id

    def _delete_session_events(self, *, actor_id: str, session_id: str) -> int:
        """Delete every short-term event for one (actor, session).

        The SEMANTIC extraction strategy re-derives long-term records from the
        retained conversation events. Deleting only the long-term record lets
        the next consolidation cycle re-extract the same lesson under a fresh
        id (the "delete then it comes back" bug). Removing the source events
        for the session leaves nothing to re-extract.

        Best-effort: a failure to list/delete events is logged and the count so
        far returned — the long-term record is already gone, so we never raise
        and turn a mostly-successful delete into a 500.

        Returns:
            The number of events deleted.
        """
        client = self._get_client()
        deleted = 0
        next_token: Optional[str] = None
        try:
            while True:
                kwargs: Dict[str, Any] = {
                    "memoryId": self._memory_id,
                    "actorId": actor_id,
                    "sessionId": session_id,
                    "maxResults": _AGENTCORE_MAX_RESULTS,
                }
                if next_token:
                    kwargs["nextToken"] = next_token
                page = client.list_events(**kwargs)
                for event in page.get("events", []) or []:
                    event_id = event.get("eventId")
                    if not event_id:
                        continue
                    client.delete_event(
                        memoryId=self._memory_id,
                        actorId=actor_id,
                        sessionId=session_id,
                        eventId=event_id,
                    )
                    deleted += 1
                next_token = page.get("nextToken")
                if not next_token:
                    break
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            logger.error(
                "failed deleting source events for actor=%s session=%s "
                "(deleted %d so far): %s",
                actor_id, session_id, deleted, exc,
            )
        return deleted

    def delete_record(self, *, memory_record_id: str) -> None:
        """Delete one long-term record AND the session events it was derived from.

        Deleting only the long-term record is not durable: the memory
        resource's SEMANTIC strategy re-extracts the same lesson from the
        retained conversation events on its next consolidation cycle, so the
        row reappears (with a new id) when the admin reloads the tab. We
        therefore look up the record's namespace first to recover its
        ``actorId``/``sessionId``, delete the record, then delete that
        session's source events so there is nothing left to re-extract.

        Raises:
            ValueError: when the service is not configured (defense-in-depth
                so admin DELETEs don't silently no-op).
        """
        if not self.configured:
            raise ValueError("LESSONS_MEMORY_ID is not configured")

        client = self._get_client()

        # Resolve the namespace BEFORE deleting the record — afterwards the
        # record is gone and we can't recover the actor/session. Best-effort:
        # if the lookup fails we still delete the record below (the original
        # behaviour), just without the event cleanup.
        actor_session: Optional[tuple] = None
        try:
            record = client.get_memory_record(
                memoryId=self._memory_id,
                memoryRecordId=memory_record_id,
            )
            namespaces = (
                record.get("memoryRecord", {}).get("namespaces", []) or []
            )
            if namespaces:
                actor_session = self._actor_session_from_namespace(namespaces[0])
            if actor_session is None:
                logger.warning(
                    "could not derive actor/session from namespaces=%s for "
                    "record=%s — deleting record only (lesson may re-extract)",
                    namespaces, memory_record_id,
                )
        except Exception as exc:  # noqa: BLE001 — fall back to record-only delete
            logger.error(
                "get_memory_record failed for %s (deleting record only): %s",
                memory_record_id, exc,
            )

        client.delete_memory_record(
            memoryId=self._memory_id,
            memoryRecordId=memory_record_id,
        )

        if actor_session is not None:
            actor_id, session_id = actor_session
            count = self._delete_session_events(
                actor_id=actor_id, session_id=session_id,
            )
            logger.info(
                "deleted lesson record=%s and %d source event(s) "
                "(actor=%s session=%s)",
                memory_record_id, count, actor_id, session_id,
            )

