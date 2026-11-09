# agents/shared/delta_fingerprint.py
"""Pure, dependency-free delta-fingerprint helpers shared by the build agents
and the REST-API delta planner. No AWS calls here — see write_table_fingerprint
in the next task for the DynamoDB side."""

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

# Cosmetic scalar aliases: catalog-specific spellings that mean the same physical
# type. Mapping these to a canonical name stops a harmless rename (e.g. varchar
# vs string) from reading as a schema delta. These are ONLY applied to bare
# scalar types — complex (array/map/struct) and decimal are handled separately so
# their semantic parameters (element type, precision/scale) are preserved.
_TYPE_ALIASES = {
    "varchar": "string",
    "char": "string",
    "text": "string",
    "integer": "int",
    "int4": "int",
    "int8": "bigint",
    "long": "bigint",
    "float8": "double",
    "real": "float",
    "bool": "boolean",
    "numeric": "decimal",
}


def _normalize_scalar(base: str) -> str:
    """Normalize a bare scalar type token via the cosmetic alias map.

    Drops any length parameter (e.g. 'varchar(20)' -> 'varchar') because length is
    cosmetic in Glue/Athena, then maps known aliases to a canonical name.

    Args:
        base: A single lower-cased type token, possibly with a length suffix.

    Returns:
        The canonical scalar type name (e.g. 'string', 'int', 'bigint').
    """
    # Length on a scalar (varchar(20)) is cosmetic in Glue/Athena — drop it.
    root = base.split("(", 1)[0].strip()
    return _TYPE_ALIASES.get(root, root)


def _split_top_level(inner: str) -> List[str]:
    """Split a type list on commas that are NOT nested inside angle brackets.

    Args:
        inner: A comma-separated type list (already whitespace-stripped).

    Returns:
        The list of top-level type substrings.
    """
    parts: List[str] = []
    depth = 0
    current = ""
    for ch in inner:
        if ch == "<":
            depth += 1
            current += ch
        elif ch == ">":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            # Top-level separator between element/field types — split here.
            parts.append(current)
            current = ""
        else:
            current += ch
    if current:
        parts.append(current)
    return parts


def _normalize_inner(inner: str) -> str:
    """Normalize the comma-separated inner type list of a complex type.

    Splits on top-level commas only (so nested generics like
    'map<string,array<int>>' are not split at the inner comma) and recursively
    applies normalize_type to each element so inner aliases collapse while genuine
    element-type changes remain visible.

    Args:
        inner: The already-lower-cased, whitespace-stripped content between the
            outer angle brackets of a complex type.

    Returns:
        The comma-joined, recursively normalized inner type list.
    """
    normalized_parts = []
    for part in _split_top_level(inner):
        # struct fields look like 'fieldname:fieldtype' — keep the name, normalize
        # only the type. A top-level ':' (depth 0) that is not part of a nested
        # generic signals a struct field.
        colon_idx = _top_level_colon(part)
        if colon_idx is not None:
            field_name = part[:colon_idx]
            field_type = part[colon_idx + 1 :]
            normalized_parts.append(f"{field_name}:{normalize_type(field_type)}")
        else:
            # Recurse: each part is itself a full type (scalar or complex).
            normalized_parts.append(normalize_type(part))
    return ",".join(normalized_parts)


def _top_level_colon(part: str) -> int:
    """Find the index of a top-level ':' separating a struct field name from type.

    Args:
        part: A single element from a complex type's inner list.

    Returns:
        The index of the first ':' outside any angle brackets, or None if absent.
    """
    depth = 0
    for i, ch in enumerate(part):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == ":" and depth == 0:
            return i
    return None


def normalize_type(raw: str) -> str:
    """Normalize a column type so cosmetic catalog aliases don't read as a delta,
    while PRESERVING semantically meaningful parameters.

    Cosmetic (collapsed): scalar aliases and scalar length — 'varchar' == 'string',
    'int' == 'integer', 'varchar(20)' == 'string'. Semantic (preserved): decimal
    precision/scale — 'decimal(10,2)' != 'decimal(38,0)'; complex element types —
    'array<int>' != 'array<string>' (but inner aliases still apply, so
    'array<varchar>' == 'array<string>').

    Args:
        raw: The raw column type string from the catalog (e.g. 'VARCHAR(20)',
            'decimal(10, 2)', 'array< int >'). May be None or empty.

    Returns:
        The canonical, lower-cased, whitespace-stripped type string. Returns '' for
        a None or empty input (never raises).
    """
    # Lower-case and strip ALL internal whitespace so 'DECIMAL(10, 2)' and
    # 'array< int >' canonicalize identically regardless of catalog formatting.
    cleaned = re.sub(r"\s+", "", (raw or "").strip().lower())
    if not cleaned:
        return ""

    # (1) Complex types: array<...>, map<...>, struct<...>. Preserve the
    # parameterized form but recursively normalize the inner element type(s).
    complex_match = re.match(r"^(array|map|struct)<(.*)>$", cleaned)
    if complex_match:
        container = complex_match.group(1)
        inner = complex_match.group(2)
        return f"{container}<{_normalize_inner(inner)}>"

    # (2) Decimal/numeric with precision/scale: preserve p,s (already space-stripped)
    # and canonicalize the 'numeric' alias to 'decimal'. 'decimal(10,2)' stays
    # distinct from 'decimal(38,0)'.
    decimal_match = re.match(r"^(decimal|numeric)\((.*)\)$", cleaned)
    if decimal_match:
        return f"decimal({decimal_match.group(2)})"

    # (3) Bare scalar: drop cosmetic length and apply the alias map.
    return _normalize_scalar(cleaned)


def merge_columns(
    columns: List[Dict[str, str]], partition_keys: List[Dict[str, str]]
) -> List[Dict[str, str]]:
    """Canonical column set for hashing: data columns followed by partition keys.

    This is THE single ordering both the planner and the agents must use. The
    agents' get_single_table_schema already appends partition keys into `columns`
    in this exact order (data cols first, then PKs); GlueService.get_table_metadata
    returns them as two separate lists, so the planner calls this to reproduce the
    identical set. Without this, a partitioned table's planner-hash and agent-hash
    differ and it reads as schemaChanged on every check (rebuild storm).

    Args:
        columns: Data columns (each a dict with at least 'name' and 'type').
        partition_keys: Partition-key columns in catalog order. May be None/empty.

    Returns:
        A new list with data columns first, then partition keys.
    """
    return list(columns) + list(partition_keys or [])


def schema_hash(columns: List[Dict[str, str]]) -> str:
    """Return a stable sha256 over ordered (name, normalized_type) pairs.

    Deliberately EXCLUDES comment text so the agent's own description write-back
    does not flip the hash and trigger a phantom rebuild on the next check.
    Callers pass the ALREADY-merged column set (see merge_columns) — this function
    does not know about partition keys, it just hashes what it is given in order.

    Args:
        columns: The ordered column set to hash (each a dict with 'name' and
            'type'; any 'comment' key is ignored).

    Returns:
        A 'sha256:'-prefixed hex digest string.
    """
    canonical = "\n".join(
        f"{str(c.get('name', '')).lower()}:{normalize_type(str(c.get('type', '')))}"
        for c in columns
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_fingerprint(
    *,
    schema_h: str,
    data_marker: str,
    marker_kind: str,
    version: str,
    catalog_id: str = "AWSDataCatalog",
) -> Dict[str, Any]:
    """Build a fingerprint dict for one table (see design data model).

    catalog_id is stored so removal cleanup can resolve the S3 key / Neptune
    coordinate for a table that is no longer in dataSources.

    Args:
        schema_h: The schema_hash() value for the table's canonical column set.
        data_marker: The data-change marker (Iceberg snapshot-id or DDB count string).
        marker_kind: Which marker kind produced data_marker ('iceberg_snapshot'|'ddb_count').
        version: The layer version this fingerprint was built under (e.g. 'v3').
        catalog_id: The Athena/Glue catalog the table lives in.

    Returns:
        A dict with schemaHash, dataMarker, markerKind, builtInVersion, catalogId, builtAt.
    """
    return {
        "schemaHash": schema_h,
        "dataMarker": data_marker,
        "markerKind": marker_kind,
        "builtInVersion": version,
        "catalogId": catalog_id,
        "builtAt": datetime.now(timezone.utc).isoformat(),
    }


def write_table_fingerprint(
    table, *, layer_id: str, version: str, db_table: str, fp: Dict[str, Any]
) -> None:
    """SET one nested tableFingerprints.<db.table> entry on the config record.

    Uses a nested-map SET (not a whole-map overwrite) so concurrent per-table
    writes never clobber each other. This MUST be two separate UpdateItem calls:
    DynamoDB rejects a single expression that both writes ``tableFingerprints`` and
    its child path ``tableFingerprints.<key>`` with "Two document paths overlap".
    So we (1) initialize the parent map only if it is absent, then (2) set the
    child path. Step 1 is a no-op once the map exists, so step 2 never clobbers a
    sibling table's entry.

    Args:
        table: A boto3 DynamoDB Table resource (or a mock with .update_item).
        layer_id: The config record partition key (id).
        version: The config record sort key (version, e.g. 'v3').
        db_table: The 'database.table' key under tableFingerprints.
        fp: The fingerprint dict (see make_fingerprint).

    Returns:
        None.
    """
    key = {"id": layer_id, "version": version}
    # Step 1 — ensure the parent map exists (no-op if already present).
    table.update_item(
        Key=key,
        UpdateExpression="SET tableFingerprints = if_not_exists(tableFingerprints, :empty)",
        ExpressionAttributeValues={":empty": {}},
    )
    # Step 2 — set only this table's nested entry (leaves siblings untouched).
    table.update_item(
        Key=key,
        UpdateExpression="SET tableFingerprints.#dt = :fp",
        ExpressionAttributeNames={"#dt": db_table},
        ExpressionAttributeValues={":fp": fp},
    )


def read_stored_fingerprints(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return the tableFingerprints map from a config record (empty if absent).

    Args:
        config: A semantic-layer-metadata config item (dict).

    Returns:
        The tableFingerprints map, or {} when the key is missing/None.
    """
    return dict(config.get("tableFingerprints") or {})


def iceberg_snapshot_id(*, db: str, tbl: str, catalog_id: str, region: str) -> str:
    """Current Iceberg snapshot-id via pyiceberg (exact data marker).

    Fail-soft: returns 'NA' on any error so a transient S3 Tables hiccup classifies
    the table as unchanged rather than force-rebuilding it. boto3/pyiceberg imported
    lazily so this module stays import-light for pure-logic tests.

    Args:
        db: Glue/Iceberg database (namespace) name.
        tbl: Table name.
        catalog_id: 's3tablescatalog/<bucket>' form.
        region: AWS region.

    Returns:
        The snapshot-id as a string, 'empty' if the table has no snapshot, or 'NA' on error.
    """
    import logging as _lg

    try:
        import boto3
        from pyiceberg.catalog import load_catalog

        bucket = catalog_id.split("/", 1)[1]
        acct = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
        cat = load_catalog(
            "s3tables",
            **{
                "type": "rest",
                "uri": f"https://s3tables.{region}.amazonaws.com/iceberg",
                "warehouse": f"arn:aws:s3tables:{region}:{acct}:bucket/{bucket}",
                "rest.sigv4-enabled": "true",
                "rest.signing-region": region,
                "rest.signing-name": "s3tables",
            },
        )
        snap = cat.load_table((db, tbl)).current_snapshot()
        return str(snap.snapshot_id) if snap else "empty"
    except Exception as e:  # noqa: BLE001
        _lg.getLogger(__name__).warning("Iceberg snapshot failed %s.%s: %s", db, tbl, e)
        return "NA"


def is_count_free_catalog(*, catalog_id: str, region: str) -> bool:
    """True when this Athena catalog must NOT be asked for a ``COUNT(*)``.

    A Lambda-backed (``Type == 'LAMBDA'``) catalog that is not the DynamoDB
    connector gets no aggregation pushdown, so ``COUNT(*)`` degrades into a full
    remote scan streamed through the connector Lambda. The DynamoDB connector is
    excluded because its count path is already handled (and bounded) by
    :func:`ddb_count_marker`, which callers have relied on since before this gate.

    Fail-soft on lookup failure: returns False so behaviour is unchanged from before
    this gate existed, rather than silently disabling delta detection everywhere.

    Args:
        catalog_id: Athena catalog id (e.g. 'snowflake_fhir_db').
        region: AWS region for the Athena client.

    Returns:
        True if a COUNT(*) against this catalog must be skipped.
    """
    import logging as _lg

    import boto3

    if not catalog_id or catalog_id in ("AWSDataCatalog", "AwsDataCatalog"):
        return False
    if catalog_id.startswith("s3tablescatalog/"):
        return False
    if "dynamodb" in catalog_id.lower():
        return False
    try:
        cat = boto3.client("athena", region_name=region).get_data_catalog(
            Name=catalog_id
        )["DataCatalog"]
    except Exception as e:  # noqa: BLE001
        _lg.getLogger(__name__).warning(
            "Could not resolve Athena catalog '%s' type (%s); assuming a COUNT(*) "
            "is safe, which is the pre-existing behaviour.",
            catalog_id,
            e,
        )
        return False
    return str(cat.get("Type", "")).upper() == "LAMBDA"


def unmeasurable_data_marker(*, db: str, tbl: str, catalog_id: str) -> str:
    """Data marker for a federated catalog whose row count cannot be measured.

    Some Lambda-backed connectors (e.g. the Snowflake connector) do NOT receive
    aggregation pushdown: a ``COUNT(*)`` streams every row of the remote table
    through the connector Lambda and exceeds its 900s ceiling. Worse, the abandoned
    invocations keep running and saturate the connector, so the NEXT query fails
    with a misleading ``TABLE_NOT_FOUND``. We therefore refuse to measure.

    The returned marker embeds the current timestamp, so it never compares equal to
    a previously stored marker and the table is always classified ``dataChanged``.
    That direction is deliberate: an unnecessary rebuild is merely expensive, while
    a table wrongly reported ``unchanged`` would silently serve stale semantics.
    A stable placeholder would do exactly that, so it is NOT used here.

    Args:
        db: Database name (recorded for operator legibility only).
        tbl: Table name (recorded for operator legibility only).
        catalog_id: Athena catalog id (recorded for operator legibility only).

    Returns:
        A marker string 'unmeasurable=<catalog>.<db>.<tbl>;at=<iso8601>' that is
        distinct on every call.
    """
    import logging as _lg

    _lg.getLogger(__name__).warning(
        "Data marker for %s.%s on catalog '%s' is UNMEASURABLE (federated connector "
        "without aggregation pushdown); classifying the table as changed so it is "
        "always rebuilt rather than wrongly reported unchanged.",
        db,
        tbl,
        catalog_id,
    )
    stamp = datetime.now(timezone.utc).isoformat()
    return f"unmeasurable={catalog_id}.{db}.{tbl};at={stamp}"


def ddb_count_marker(
    *, db: str, tbl: str, catalog_id: str, region: str, artifacts_bucket: str
) -> str:
    """Coarse data marker for DynamoDB-connector tables: DescribeTable ItemCount
    (free, ~6h stale) + bounded Athena COUNT(*). Documented limitation: same-count
    value churn is NOT detected.

    Args:
        db: Glue database name (Athena query context).
        tbl: Table name (also the DynamoDB table name for describe_table).
        catalog_id: Athena catalog id for the COUNT(*) query context.
        region: AWS region.
        artifacts_bucket: S3 bucket for Athena results output location.

    Returns:
        A marker string 'items=<n>;count=<n>' (either side may be 'NA' on failure).
    """
    import logging as _lg

    import boto3

    log = _lg.getLogger(__name__)
    items = "NA"
    try:
        items = str(
            boto3.client("dynamodb", region_name=region)
            .describe_table(TableName=tbl)["Table"]
            .get("ItemCount", "NA")
        )
    except Exception as e:  # noqa: BLE001
        log.warning("DDB describe_table failed for %s: %s", tbl, e)
    try:
        count = _athena_scalar(
            f'SELECT COUNT(*) FROM "{db}"."{tbl}"',
            catalog_id=catalog_id,
            database=db,
            region=region,
            artifacts_bucket=artifacts_bucket,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Athena COUNT(*) failed for %s.%s: %s", db, tbl, e)
        count = "NA"
    return f"items={items};count={count}"


def _athena_scalar(
    query: str,
    *,
    catalog_id: str,
    database: str,
    region: str,
    artifacts_bucket: str,
) -> str:
    """Run a single-value Athena query; return the scalar as a string. Reuses the
    ATHENA_WORKGROUP + <artifacts_bucket>/athena-results/ convention the agents use.

    Args:
        query: A single-scalar SQL query (e.g. SELECT COUNT(*) ...).
        catalog_id: Athena catalog; added to QueryExecutionContext unless default.
        database: Athena database for the query context.
        region: AWS region.
        artifacts_bucket: S3 bucket for the results OutputLocation.

    Returns:
        The scalar VarCharValue as a string, or 'NA' if no row.

    Raises:
        RuntimeError: if the Athena query does not reach SUCCEEDED.
    """
    import os
    import time

    import boto3

    athena = boto3.client("athena", region_name=region)
    ctx = {"Database": database}
    if catalog_id and catalog_id not in ("AWSDataCatalog", "AwsDataCatalog"):
        ctx["Catalog"] = catalog_id
    qid = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext=ctx,
        WorkGroup=os.environ.get("ATHENA_WORKGROUP", "primary"),
        ResultConfiguration={
            "OutputLocation": f"s3://{artifacts_bucket}/athena-results/"
        },
    )["QueryExecutionId"]
    st = "RUNNING"
    for _ in range(30):
        st = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"][
            "Status"
        ]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)  # nosemgrep: arbitrary-sleep - Athena status poll
    if st != "SUCCEEDED":
        # Cancel before giving up. Abandoning a still-running query is not merely
        # untidy: against a federated connector it keeps streaming rows for up to
        # the Lambda's 900s ceiling, per split, holding concurrency the whole time.
        # A caller that loops over tables leaves one such scan behind per table,
        # which saturates the connector and makes the NEXT query fail with a
        # misleading TABLE_NOT_FOUND (a load symptom that reads as a schema error).
        if st in ("QUEUED", "RUNNING"):
            import logging as _lg

            try:
                athena.stop_query_execution(QueryExecutionId=qid)
                _lg.getLogger(__name__).warning(
                    "Cancelled Athena query %s after 60s in state %s", qid, st
                )
            except Exception as cancel_err:  # noqa: BLE001
                _lg.getLogger(__name__).warning(
                    "Could not cancel Athena query %s: %s", qid, cancel_err
                )
        raise RuntimeError(f"Athena COUNT state={st} (query_id={qid})")
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    return rows[1]["Data"][0].get("VarCharValue", "NA") if len(rows) > 1 else "NA"
