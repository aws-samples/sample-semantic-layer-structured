# lambda/rest-api/services/delta_planner_service.py
"""On-demand delta planner: fingerprints selected tables and diffs against the
fingerprints stored in the semantic-layer-metadata config record. Pure read-side
— no agent invocation here."""

import logging
import os
from typing import Any, Dict, List

import boto3

# Reuse the SAME pure helpers the agents use, so a fresh fingerprint computed
# here is byte-identical to one the agent would write.
try:
    from agents.shared.delta_fingerprint import (
        schema_hash,
        make_fingerprint,
        merge_columns,
        iceberg_snapshot_id,
        ddb_count_marker,
        is_count_free_catalog,
        unmeasurable_data_marker,
    )
except ImportError:  # Lambda container flattens the path
    from shared.delta_fingerprint import (  # type: ignore
        schema_hash,
        make_fingerprint,
        merge_columns,
        iceberg_snapshot_id,
        ddb_count_marker,
        is_count_free_catalog,
        unmeasurable_data_marker,
    )

# Same FK-plan helpers the ontology agent uses (Phase 2), so the planner's
# blast-radius closure agrees with what the agent built.
try:
    from agents.shared.fk_plan import build_fk_plan, fk_closure
except ImportError:  # Lambda container flattens the path
    from shared.fk_plan import build_fk_plan, fk_closure  # type: ignore

logger = logging.getLogger(__name__)


class DeltaPlannerService:
    """Computes the per-table delta plan for a semantic layer (read-side only)."""

    def __init__(self, *, metadata_table, glue_service, artifacts_bucket, region):
        """Store injected dependencies.

        Args:
            metadata_table: boto3 DynamoDB Table resource for semantic-layer-metadata.
            glue_service: A GlueService instance (for get_table_metadata).
            artifacts_bucket: S3 bucket name holding metadata docs + ontology fragments.
            region: AWS region string.
        """
        self._table = metadata_table
        self.glue_service = glue_service
        self.artifacts_bucket = artifacts_bucket
        self.region = region

    def _fingerprint_table(self, ds: Dict[str, Any], *, version: str) -> Dict[str, Any]:
        """Compute the current fingerprint for one selected data-source entry.

        Args:
            ds: A dataSources entry with databaseName, tableName, catalogId.
            version: The layer version to stamp on the fingerprint.

        Returns:
            The fingerprint dict (see make_fingerprint).
        """
        db = ds["databaseName"]
        tbl = ds["tableName"]
        cat = ds.get("catalogId") or "AWSDataCatalog"
        meta = self.glue_service.get_table_metadata(db, tbl, cat)
        # GlueService returns columns + partitionKeys as SEPARATE lists; the agents'
        # get_single_table_schema appends PKs into `columns`. merge_columns reproduces
        # the agent's exact ordered set so planner-hash == agent-hash (see CR-1 / Task 1).
        s_hash = schema_hash(
            merge_columns(meta.get("columns", []), meta.get("partitionKeys", []))
        )
        if str(cat).startswith("s3tablescatalog/"):
            marker, kind = self._iceberg_snapshot_id(db, tbl, cat), "iceberg_snapshot"
        elif is_count_free_catalog(catalog_id=str(cat), region=self.region):
            # Mirrors the agents' _data_marker_for_table: a non-DynamoDB Lambda catalog
            # (e.g. Snowflake) gets no aggregation pushdown, so a COUNT(*) would become
            # a full remote scan. The marker is always-distinct, so such a table is
            # always planned as dataChanged rather than wrongly reported unchanged.
            marker, kind = (
                unmeasurable_data_marker(db=db, tbl=tbl, catalog_id=str(cat)),
                "unmeasurable",
            )
        else:
            marker, kind = self._ddb_count_marker(db, tbl, cat), "ddb_count"
        return make_fingerprint(
            schema_h=s_hash,
            data_marker=marker,
            marker_kind=kind,
            version=version,
            catalog_id=cat,
        )

    def _iceberg_snapshot_id(self, db, tbl, catalog_id):
        """Delegate to the shared iceberg_snapshot_id (kept as a method so tests can patch it)."""
        return iceberg_snapshot_id(
            db=db, tbl=tbl, catalog_id=catalog_id, region=self.region
        )

    def _ddb_count_marker(self, db, tbl, catalog_id):
        """Delegate to the shared ddb_count_marker (kept as a method so tests can patch it)."""
        return ddb_count_marker(
            db=db,
            tbl=tbl,
            catalog_id=catalog_id,
            region=self.region,
            artifacts_bucket=self.artifacts_bucket,
        )

    def _diff(
        self, current: Dict[str, Dict], stored: Dict[str, Dict]
    ) -> Dict[str, List[str]]:
        """Classify each table into exactly one bucket (design precedence).

        Precedence: removed (in stored, not current) > added (in current, not stored)
        > schemaChanged (schemaHash differs) > dataChanged (dataMarker differs) >
        unchanged. Lists are sorted for deterministic output.

        Args:
            current: {db.table: fingerprint} freshly computed this run.
            stored: {db.table: fingerprint} from the config record's last build.

        Returns:
            Dict with keys added, removed, schemaChanged, dataChanged, unchanged,
            each a sorted list of 'db.table' names.
        """
        buckets = {
            "added": [],
            "removed": [],
            "schemaChanged": [],
            "dataChanged": [],
            "unchanged": [],
        }
        for dt in sorted(set(stored) - set(current)):
            buckets["removed"].append(dt)
        for dt in sorted(current):
            if dt not in stored:
                buckets["added"].append(dt)
            elif current[dt].get("schemaHash") != stored[dt].get("schemaHash"):
                buckets["schemaChanged"].append(dt)
            elif current[dt].get("dataMarker") != stored[dt].get("dataMarker"):
                buckets["dataChanged"].append(dt)
            else:
                buckets["unchanged"].append(dt)
        return buckets

    def _load_last_built_fk_plan(
        self, layer_id: str
    ) -> Dict[str, List[Dict[str, str]]]:
        """Rebuild the FK plan from the last-built Phase-1 fragments in S3.

        Fragments live at ontologies/{layer_id}/phase1/*.md; each has a
        '**Table:** <name>' line and a '**FK Hints for Phase 2:** <hints>' line
        (see save_intermediate_ontology / load_phase1_fragments in the ontology agent).
        Fail-soft: returns {} if no fragments exist (first build) — then the closure
        is a no-op and only directly-changed tables rebuild.

        Args:
            layer_id: The ontology/layer id (S3 prefix component).

        Returns:
            The FK plan {table: [{fk_column, target_table}]} via build_fk_plan.
        """
        import re

        s3 = boto3.client("s3", region_name=self.region)
        prefix = f"ontologies/{layer_id}/phase1/"
        fragments = {"tables": []}
        try:
            paged = s3.get_paginator("list_objects_v2")
            for page in paged.paginate(Bucket=self.artifacts_bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    body = s3.get_object(Bucket=self.artifacts_bucket, Key=obj["Key"])
                    text = body["Body"].read().decode("utf-8", errors="ignore")
                    # Use the SAME regexes as load_phase1_fragments (agents/ontology_agent
                    # /main.py) so parsing is identical on both sides — `(.+)` + strip.
                    name_m = re.search(r"\*\*Table:\*\* (.+)", text)
                    hints_m = re.search(r"\*\*FK Hints for Phase 2:\*\* (.+)", text)
                    if not name_m:
                        continue
                    hints = hints_m.group(1).strip() if hints_m else ""
                    if hints.lower() == "none":
                        hints = ""
                    fragments["tables"].append(
                        {"table_name": name_m.group(1).strip(), "fk_hints": hints}
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not load Phase-1 fragments for %s: %s", layer_id, e)
        return build_fk_plan(fragments)

    def _expand_fk_closure(self, layer_id: str, changed_bare: set):
        """Return (rebuild_set, fk_neighbors) using bare table names.

        Args:
            layer_id: The ontology/layer id.
            changed_bare: Set of bare (no db prefix) table names that changed.

        Returns:
            (closure, neighbors): closure = changed_bare plus FK neighbors;
            neighbors = closure minus changed_bare (the tables pulled in purely
            because they neighbor a changed table).
        """
        fk_plan = self._load_last_built_fk_plan(layer_id)
        closure = fk_closure(set(changed_bare), fk_plan)
        neighbors = closure - set(changed_bare)
        return closure, neighbors

    def compute_plan(self, layer_id: str, *, layer_type: str) -> Dict[str, Any]:
        """Compute the DeltaPlan for a layer. layer_type in {'VKG','SemanticRAG'}.

        Loads the config (active version), fingerprints every selected table, diffs
        against stored fingerprints, (VKG) expands the changed set to the FK-neighbor
        closure, and builds rebuildSet + perTable reasons.

        Args:
            layer_id: The layer/config id.
            layer_type: 'VKG' or 'SemanticRAG' — only VKG expands FK neighbors.

        Returns:
            A DeltaPlan dict: added/removed/schemaChanged/dataChanged/fkNeighbors/
            unchanged (sorted lists of 'db.table'), rebuildSet (sorted), perTable
            ({db.table: {reason, [detail]}}).

        Raises:
            ValueError: if the layer config is not found.
        """
        item = self._table.get_item(
            Key={"id": layer_id, "version": self._active_version(layer_id)}
        )
        config = item.get("Item")
        if not config:
            raise ValueError(f"layer config not found: {layer_id}")
        version = config["version"]
        stored = dict(config.get("tableFingerprints") or {})
        current = {}
        for ds in config.get("dataSources", []):
            if ds.get("databaseName") and ds.get("tableName"):
                dt = f"{ds['databaseName']}.{ds['tableName']}"
                current[dt] = self._fingerprint_table(ds, version=version)
        buckets = self._diff(current, stored)
        per_table = {}
        for dt in buckets["added"]:
            per_table[dt] = {"reason": "added"}
        for dt in buckets["schemaChanged"]:
            per_table[dt] = {"reason": "schema"}
        for dt in buckets["dataChanged"]:
            per_table[dt] = {"reason": "data"}
        for dt in buckets["removed"]:
            per_table[dt] = {"reason": "removed"}

        rebuild = set(
            buckets["added"] + buckets["schemaChanged"] + buckets["dataChanged"]
        )
        fk_neighbors: List[str] = []
        if layer_type == "VKG" and rebuild:
            bare_to_full = {dt.split(".", 1)[1]: dt for dt in current}
            changed_bare = {dt.split(".", 1)[1] for dt in rebuild}
            _closure_bare, neighbors_bare = self._expand_fk_closure(
                layer_id, changed_bare
            )
            for bare in neighbors_bare:
                full = bare_to_full.get(bare)
                if full:
                    rebuild.add(full)
                    fk_neighbors.append(full)
                    per_table[full] = {"reason": "fk-neighbor"}

        # DDB coarse-marker caveat surfaced honestly (design "fail loudly").
        for dt, fp in current.items():
            if fp.get("markerKind") == "ddb_count" and dt in per_table:
                per_table[dt]["detail"] = (
                    "data marker is approximate (row count); value-only changes "
                    "at a stable count are not detected"
                )

        return {
            "added": buckets["added"],
            "removed": buckets["removed"],
            "schemaChanged": buckets["schemaChanged"],
            "dataChanged": buckets["dataChanged"],
            "fkNeighbors": sorted(fk_neighbors),
            "unchanged": buckets["unchanged"],
            "rebuildSet": sorted(rebuild),
            "perTable": per_table,
        }

    def _active_version(self, layer_id: str) -> str:
        """Highest numeric version for the layer (mirror agents' _resolve_active_version).

        Defensive: a malformed version string (no digits) must not crash the plan —
        treat it as version 0 and fall back to 'v1' if the query fails or is empty.

        Args:
            layer_id: The layer/config id (partition key).

        Returns:
            The active (highest numeric) version string, or 'v1' as a fallback.
        """
        from boto3.dynamodb.conditions import Key as DKey
        import re as _re

        def _num(v: str) -> int:
            m = _re.search(r"\d+", v or "")
            return int(m.group()) if m else 0

        try:
            resp = self._table.query(
                KeyConditionExpression=DKey("id").eq(layer_id),
                ProjectionExpression="version",
            )
            items = resp.get("Items", [])
            if items:
                return max(items, key=lambda i: _num(i.get("version", "")))["version"]
        except Exception as e:  # noqa: BLE001
            logger.warning("_active_version fallback to v1 for %s: %s", layer_id, e)
        return "v1"

    def cleanup_removed(
        self, layer_id: str, *, version: str, layer_type: str, removed: List
    ) -> None:
        """Delete artifacts for removed tables.

        Args:
            layer_id: The layer/config id.
            version: The layer version (for the RAG S3 key path).
            layer_type: 'SemanticRAG' (delete KB doc + sidecar) or 'VKG'
                (delete Neptune triples + the S3 Phase-1 fragment).
            removed: A list of (database, table, catalog_id) tuples.

        Returns:
            None. Fail-soft per table (a delete failure is logged, not raised).
        """
        s3 = boto3.client("s3", region_name=self.region)
        for db, tbl, cat in removed:
            if layer_type == "SemanticRAG":
                key = f"metadata/{layer_id}/{version}/{cat}/{db}/{tbl}.md"
                for k in (key, f"{key}.metadata.json"):
                    try:
                        s3.delete_object(Bucket=self.artifacts_bucket, Key=k)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("cleanup: could not delete %s: %s", k, e)
            else:  # VKG
                try:
                    self._neptune_delete_table(layer_id, db, tbl)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "cleanup: Neptune delete failed %s.%s: %s", layer_id, tbl, e
                    )
                try:
                    self._delete_phase1_fragment(layer_id, db, tbl)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "cleanup: fragment delete failed %s.%s: %s", layer_id, tbl, e
                    )

    def _neptune_delete_table(
        self, layer_id: str, database: str, table_name: str
    ) -> None:
        """Call the Neptune Gateway delete_table_triples tool via MCP.

        Passes the DATABASE too — the mapsToTable object is the literal
        "<database>.<table>" (see Task 11), so the tool needs both.

        Args:
            layer_id: The layer/ontology id.
            database: The removed table's database.
            table_name: The removed table's name.

        Returns:
            None.

        Raises:
            RuntimeError: if NEPTUNE_GATEWAY_URL is not set.
        """
        import uuid as _uuid
        from strands.tools.mcp import MCPClient
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

        gateway_url = os.environ.get("NEPTUNE_GATEWAY_URL", "")
        if not gateway_url:
            raise RuntimeError("NEPTUNE_GATEWAY_URL not set")
        with MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=gateway_url,
                aws_region=self.region,
                aws_service="bedrock-agentcore",
            )
        ) as client:
            client.call_tool_sync(
                str(_uuid.uuid4()),
                "delete-table-triples___delete_table_triples",
                {
                    "ontology_id": layer_id,
                    "database_name": database,
                    "table_name": table_name,
                },
            )

    def _delete_phase1_fragment(self, layer_id: str, database: str, table_name: str) -> None:
        """Delete the removed table's Phase-1 fragment so assembly won't re-include it.

        The filename is `table-{step:02d}-{table_name}.md` (see
        save_intermediate_ontology) under `ontologies/{layer_id}/phase1/`; the planner
        does not know the step number, so it lists the prefix. Matching on the bare
        `-{table_name}.md` suffix ALONE is unsafe: two databases in the same layer can
        both have a table named e.g. `orders` (`table-03-orders.md`,
        `table-07-orders.md`), and removing one must NOT delete the other's fragment.
        We therefore disambiguate on the fragment BODY, which carries the
        `mapsToTable "<database>.<table>"` literal — deleting only the fragment(s)
        whose body matches the removed table's full `db.table` identity.

        Args:
            layer_id: The layer/ontology id.
            database: The removed table's database (disambiguates same-named siblings).
            table_name: The removed table's name.

        Returns:
            None.
        """
        s3 = boto3.client("s3", region_name=self.region)
        prefix = f"ontologies/{layer_id}/phase1/"
        suffix = f"-{table_name}.md"
        maps_to = f'"{database}.{table_name}"'  # the literal written in the N-Quads
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.artifacts_bucket, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(suffix):
                    continue
                # Confirm this fragment actually belongs to <database>.<table_name>
                # before deleting — protects a same-named table in another database.
                try:
                    body = s3.get_object(Bucket=self.artifacts_bucket, Key=key)[
                        "Body"
                    ].read().decode("utf-8", errors="ignore")
                except Exception as e:  # noqa: BLE001
                    logger.warning("cleanup: could not read fragment %s: %s", key, e)
                    continue
                if maps_to in body:
                    s3.delete_object(Bucket=self.artifacts_bucket, Key=key)
