"""Two-phase build-status writer for the Hybrid creation agent.

The Hybrid creation agent builds BOTH artifacts sequentially and reports a
two-phase status to DynamoDB so the frontend can poll the build:

    buildingKB  ->  buildingVKG  ->  completed        (happy path)
    buildingKB  ->  failed:kb                          (KB build failed)
    buildingKB  ->  buildingVKG  ->  failed:vkg        (VKG build failed)

The DynamoDB write lives behind an injected ``table_factory`` seam (with a
lazily-constructed boto3-backed default) so importing this module never touches
AWS and unit tests stay hermetic.

Copy-and-own: standard library + a lazily-imported ``boto3`` only. No import
from ``agents.shared`` or any sibling agent package (enforced by
``tests/unit/test_hybrid_isolation.py``).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

# A ``table_factory`` seam returns a boto3 DynamoDB Table resource for a given
# table name. Injecting it keeps import network-free and gives unit tests a
# single monkeypatch point.
TableFactory = Callable[[str], object]


def _default_table_factory(table_name: str) -> object:
    """Build a boto3-backed DynamoDB Table resource (lazy — no import-time AWS).

    Args:
        table_name: The DynamoDB table to bind to.

    Returns:
        A boto3 DynamoDB ``Table`` resource for ``table_name``.
    """
    import boto3  # Lazy import: never at module import time.

    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.resource("dynamodb", region_name=region).Table(table_name)


def _version_num(version: str) -> int:
    """Parse the integer suffix from a version string like ``'v1'`` / ``'v10'``.

    Args:
        version: A version sort-key (e.g. ``'v3'``); ``None``/empty is treated
            as ``'v0'``.

    Returns:
        The integer suffix, or ``0`` when no digits are present.
    """
    import re

    match = re.search(r"\d+", version or "v0")
    return int(match.group()) if match else 0


def _resolve_active_version(table: object, job_id: str) -> str:
    """Return the version sort-key of the highest-numbered record for ``job_id``.

    Queries every version record and returns the one whose numeric suffix is
    largest (so ``v10`` beats ``v9``). Falls back to ``'v1'`` when no record
    exists yet (first invocation of the initial build).

    Args:
        table: A boto3 DynamoDB Table resource.
        job_id: The semantic-layer id (partition key).

    Returns:
        The active version sort-key.
    """
    # Local import so this module imports without boto3 present under stubs.
    from boto3.dynamodb.conditions import Key

    try:
        resp = table.query(
            KeyConditionExpression=Key("id").eq(job_id),
            ProjectionExpression="version",
        )
        items = resp.get("Items", [])
        if items:
            return max(items, key=lambda item: _version_num(item["version"]))["version"]
    except Exception as exc:  # noqa: BLE001 — fall back to v1 on any query error
        logger.warning("_resolve_active_version fallback to v1: %s", exc)
    return "v1"


class ProgressWriter:
    """Write the Hybrid two-phase build status to DynamoDB.

    The status field mirrors the query-agent's degrade taxonomy: the phase
    tokens (``buildingKB`` / ``buildingVKG`` / ``completed`` / ``failed:<phase>``)
    are written verbatim so the frontend renders the correct progress state.
    """

    def __init__(
        self,
        *,
        job_id: str,
        version: Optional[str] = None,
        table_name: Optional[str] = None,
        table_factory: Optional[TableFactory] = None,
    ) -> None:
        """Bind the writer to one build job.

        Args:
            job_id: The semantic-layer id (DynamoDB partition key).
            version: Explicit version sort-key to target. When ``None`` the
                active (highest-numbered) version is resolved lazily on first
                write.
            table_name: DynamoDB table name. Defaults to the
                ``ONTOLOGY_METADATA_TABLE`` env var (``semantic-layer-metadata``).
            table_factory: Seam returning a boto3 Table resource. Defaults to a
                lazily-constructed boto3-backed factory (no import-time AWS).
        """
        self.job_id = job_id
        self._version = version
        self.table_name = table_name or os.environ.get(
            "ONTOLOGY_METADATA_TABLE", "semantic-layer-metadata"
        )
        self._table_factory = table_factory or _default_table_factory
        self._table: Optional[object] = None

    def _get_table(self) -> object:
        """Return (memoizing) the DynamoDB Table resource for this job."""
        if self._table is None:
            self._table = self._table_factory(self.table_name)
        return self._table

    def _resolve_version(self, table: object) -> str:
        """Return the target version, resolving the active one once when needed."""
        if self._version is None:
            self._version = _resolve_active_version(table, self.job_id)
        return self._version

    def write(
        self,
        status: str,
        *,
        fields: Optional[Dict[str, object]] = None,
        remove: Optional[Sequence[str]] = None,
    ) -> None:
        """Write a build status (plus optional progress counters) to DynamoDB.

        For a long time this wrote ONLY ``status``, which is why the build's
        completion screen reported "0 tables". The frontend reads
        ``totalTables`` / ``tablesProcessed`` / ``currentTable`` /
        ``progressPercent`` from the same record; with those attributes never
        written, the API had nothing to forward and every count rendered as 0
        even though the graph had been built correctly.

        Args:
            status: The phase token to persist (e.g. ``'buildingKB'``,
                ``'buildingVKG'``, ``'completed'``, ``'failed:kb'``).
            fields: Extra attributes to set alongside the status, e.g.
                ``{"totalTables": 8, "tablesProcessed": 3}``. Every key is
                written through a ``#k<n>`` alias so an attribute name that
                collides with a DynamoDB reserved word (``status``, ``percent``,
                …) can never break the update expression.
            remove: Attribute names to DELETE in the same update. Used on the
                terminal success write to clear a previous attempt's ``error`` /
                ``failedAt``, which would otherwise sit on a ``completed``
                record and render as a failure in the UI.

        Returns:
            None. A DynamoDB error is logged and swallowed — a status-write
            hiccup must never crash the build thread (the build's own
            success/failure is authoritative, not the progress row).
        """
        try:
            table = self._get_table()
            version = self._resolve_version(table)
            names: Dict[str, str] = {"#status": "status"}
            values: Dict[str, object] = {
                ":status": status,
                ":updated": datetime.now(timezone.utc).isoformat(),
            }
            assignments = ["#status = :status", "updatedAt = :updated"]
            for index, (key, value) in enumerate(sorted((fields or {}).items())):
                names[f"#k{index}"] = key
                values[f":v{index}"] = value
                assignments.append(f"#k{index} = :v{index}")
            expression = "SET " + ", ".join(assignments)
            if remove:
                for index, key in enumerate(remove):
                    names[f"#r{index}"] = key
                expression += " REMOVE " + ", ".join(
                    f"#r{index}" for index, _key in enumerate(remove)
                )
            table.update_item(
                Key={"id": self.job_id, "version": version},
                UpdateExpression=expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            logger.info(
                "Hybrid build status: %s (version=%s) -> %s %s",
                self.job_id, version, status, fields or {},
            )
        except Exception as exc:  # noqa: BLE001 — status write is best-effort
            logger.error("ProgressWriter.write failed (non-fatal): %s", exc)
