"""Rich Knowledge Base document builder for the Hybrid creation agent.

Writes the RICH prose (business purpose, synonyms, reference tables, sample SQL,
sample data, notes) for each table to S3 as a markdown document plus a companion
``.metadata.json`` sidecar, ready for Bedrock Knowledge Base ingestion. The thin
structural VKG (built separately by ``vkg_builder``) gets NONE of this prose —
that split is the whole point of the Hybrid design.

Copy-and-own: the S3 markdown-write + sidecar-size-guard logic is copied in shape
from ``agents/metadata_agent/main.py::save_metadata_document_to_s3`` and its KB
sidecar constant. boto3 is kept behind an injected ``s3_put`` seam (with a
lazily-constructed boto3-backed default) so importing this module never touches
AWS and unit tests stay hermetic.

Isolation note: standard library + a lazily-imported ``boto3`` only, plus this
package's ``query_prompts`` module. No import from ``agents.shared`` or any
sibling agent package (enforced by ``tests/unit/test_hybrid_isolation.py``).
"""

import json
import logging
import os
from typing import Callable, Optional

from .query_prompts import (
    build_sidecar_attributes,
)

logger = logging.getLogger(__name__)

# Bedrock Knowledge Base hard limit on the companion ``.metadata.json`` sidecar.
# A sidecar over this size is SILENTLY dropped at ingestion (surfaced only as a
# job-level "Ignored N files …" failureReason), taking the whole document out of
# the KB. Copied verbatim from metadata_agent (_KB_METADATA_MAX_BYTES).
KB_METADATA_MAX_BYTES: int = 1024

# An ``s3_put`` seam writes one object: (bucket, key, body-bytes, content_type).
# Injecting it keeps import network-free and gives unit tests a single seam.
S3Put = Callable[[str, str, bytes, str], None]

# An ``s3_get`` seam reads one object: (bucket, key) -> body-bytes. Same seam
# rationale as ``S3Put``; used by :func:`read_kb_document`.
S3Get = Callable[[str, str], bytes]


def _build_default_s3_put() -> S3Put:
    """Construct a boto3-backed ``s3_put`` writer (lazy — no import-time AWS).

    Returns:
        An ``S3Put`` callable that writes one object to S3.
    """
    import boto3  # Lazy import: never at module import time.

    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)

    def _put(bucket: str, key: str, body: bytes, content_type: str) -> None:
        """Write one object to S3."""
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)

    return _put


def _build_default_s3_get() -> S3Get:
    """Construct a boto3-backed ``s3_get`` reader (lazy — no import-time AWS).

    Returns:
        An ``S3Get`` callable that reads one object's bytes from S3.
    """
    import boto3  # Lazy import: never at module import time.

    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)

    def _get(bucket: str, key: str) -> bytes:
        """Read one object's body bytes from S3."""
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    return _get


def kb_object_key(
    *,
    semantic_layer_id: str,
    semantic_layer_version: str,
    catalog_id: str,
    database_name: str,
    table_name: str,
) -> str:
    """Build the S3 key for a table's KB markdown document.

    Copied in shape from metadata_agent's key layout so the Hybrid KB scopes
    documents by semantic-layer id + version (multiple layers/versions coexist in
    the same shared KB).

    Args:
        semantic_layer_id: DynamoDB partition key scoping the document.
        semantic_layer_version: Version sort-key (e.g. ``'v1'``).
        catalog_id: Catalog identifier segment.
        database_name: Database segment.
        table_name: Table name (the leaf, with ``.md`` suffix).

    Returns:
        The S3 object key for the ``.md`` document.
    """
    return (
        f"metadata/{semantic_layer_id}/{semantic_layer_version}/"
        f"{catalog_id}/{database_name}/{table_name}.md"
    )


def write_kb_document(
    *,
    semantic_layer_id: str,
    semantic_layer_version: str,
    database_name: str,
    table_name: str,
    catalog_id: str,
    metadata_content: str,
    bucket: Optional[str] = None,
    s3_put: Optional[S3Put] = None,
) -> str:
    """Write one table's rich KB markdown document + sidecar to S3.

    Writes two objects, scoped by semantic-layer id and version:
      s3://{bucket}/metadata/{id}/{version}/{catalog}/{database}/{table}.md
      s3://{bucket}/…/{table}.md.metadata.json

    The companion sidecar surfaces the five filter/structural attributes as
    Bedrock KB chunk metadata so the query agent can scope retrieval. The sidecar
    size is validated BEFORE writing anything (fail loud) so a pathological
    identifier can never orphan a ``.md`` with no sidecar — copied from the
    metadata agent's guard.

    Args:
        semantic_layer_id: DynamoDB partition key scoping the document.
        semantic_layer_version: Version sort-key (e.g. ``'v1'``).
        database_name: Database the table belongs to.
        table_name: Table name (used in the S3 key).
        catalog_id: Catalog identifier for this table.
        metadata_content: The rich markdown document produced upstream.
        bucket: Target S3 bucket. Defaults to the ``ARTIFACTS_BUCKET`` env var.
        s3_put: Seam writing one object. Defaults to a lazily-constructed
            boto3-backed writer (no import-time AWS).

    Returns:
        The ``s3://`` path of the written ``.md`` document.

    Raises:
        ValueError: When required scope fields are missing, no bucket is
            configured, or the sidecar exceeds the Bedrock KB size limit — fail
            loud rather than let the KB silently drop the document.
    """
    if not semantic_layer_id or not semantic_layer_version:
        raise ValueError(
            "semantic_layer_id and semantic_layer_version are required to scope "
            "the KB document"
        )
    resolved_bucket = bucket or os.environ.get("ARTIFACTS_BUCKET", "")
    if not resolved_bucket:
        raise ValueError(
            "No S3 bucket configured (pass bucket= or set ARTIFACTS_BUCKET)"
        )

    writer = s3_put or _build_default_s3_put()
    key = kb_object_key(
        semantic_layer_id=semantic_layer_id,
        semantic_layer_version=semantic_layer_version,
        catalog_id=catalog_id,
        database_name=database_name,
        table_name=table_name,
    )

    attrs = build_sidecar_attributes(
        semantic_layer_id=semantic_layer_id,
        semantic_layer_version=semantic_layer_version,
        database_name=database_name,
        table_name=table_name,
        catalog_id=catalog_id,
    )
    sidecar_body = json.dumps({"metadataAttributes": attrs}).encode("utf-8")
    if len(sidecar_body) > KB_METADATA_MAX_BYTES:
        # Fail LOUDLY: a too-long identifier would make the KB silently drop the
        # whole document at ingestion. Mirror the metadata agent's guard.
        raise ValueError(
            f"KB metadata sidecar for {database_name}.{table_name} is "
            f"{len(sidecar_body)} bytes, over the Bedrock KB limit of "
            f"{KB_METADATA_MAX_BYTES}; the document would be silently dropped at "
            f"ingestion. Shorten the table/catalog identifiers."
        )

    writer(resolved_bucket, key, metadata_content.encode("utf-8"), "text/markdown")
    writer(
        resolved_bucket,
        f"{key}.metadata.json",
        sidecar_body,
        "application/json",
    )

    s3_path = f"s3://{resolved_bucket}/{key}"
    logger.info(
        "Wrote KB document: %s (layer=%s version=%s catalog=%s)",
        s3_path, semantic_layer_id, semantic_layer_version, catalog_id,
    )
    return s3_path


def read_kb_document(
    *,
    semantic_layer_id: str,
    semantic_layer_version: str,
    database_name: str,
    table_name: str,
    catalog_id: str,
    bucket: Optional[str] = None,
    s3_get: Optional[S3Get] = None,
) -> str:
    """Read back one table's KB markdown document from S3.

    The structural VKG phase reads the documents this module wrote in the
    documentation phase in order to harvest short ``rdfs:comment`` descriptions
    from them (see ``kb_descriptions``). Reading them back from S3 — rather than
    threading the composed markdown through the build in memory — keeps the two
    phases independently runnable: a structural re-emit for an already-documented
    layer still picks the descriptions up.

    Args:
        semantic_layer_id: DynamoDB partition key scoping the document.
        semantic_layer_version: Version sort-key (e.g. ``'v1'``).
        database_name: Database the table belongs to.
        table_name: Table name (used in the S3 key).
        catalog_id: Catalog identifier for this table.
        bucket: Source S3 bucket. Defaults to the ``ARTIFACTS_BUCKET`` env var.
        s3_get: Seam reading one object. Defaults to a lazily-constructed
            boto3-backed reader (no import-time AWS).

    Returns:
        The document's markdown text.

    Raises:
        ValueError: When required scope fields are missing or no bucket is
            configured — a lookup with an empty scope would silently read the
            wrong layer's document.
        Exception: Whatever the reader raises when the object is absent
            (``NoSuchKey``). Callers that treat a missing document as "no
            descriptions to harvest" must catch it explicitly.
    """
    if not semantic_layer_id or not semantic_layer_version:
        raise ValueError(
            "semantic_layer_id and semantic_layer_version are required to scope "
            "the KB document"
        )
    resolved_bucket = bucket or os.environ.get("ARTIFACTS_BUCKET", "")
    if not resolved_bucket:
        raise ValueError(
            "No S3 bucket configured (pass bucket= or set ARTIFACTS_BUCKET)"
        )
    reader = s3_get or _build_default_s3_get()
    key = kb_object_key(
        semantic_layer_id=semantic_layer_id,
        semantic_layer_version=semantic_layer_version,
        catalog_id=catalog_id,
        database_name=database_name,
        table_name=table_name,
    )
    return reader(resolved_bucket, key).decode("utf-8")


def trigger_kb_ingestion(
    *,
    kb_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
    start_ingestion: Optional[Callable[[str, str], str]] = None,
) -> Optional[str]:
    """Fire-and-forget: start Bedrock KB ingestion after documents are written.

    Copied in shape from metadata_agent's ``_trigger_kb_ingestion``. Best-effort:
    a missing id/data-source or an ingestion error is logged and swallowed so a
    KB-refresh hiccup never fails the build.

    Args:
        kb_id: The Bedrock KB id. Defaults to ``SEMANTIC_RAG_KB_ID`` env var.
        data_source_id: The KB data-source id. Defaults to
            ``SEMANTIC_RAG_DATA_SOURCE_ID`` env var.
        start_ingestion: Seam ``(kb_id, ds_id) -> ingestion_job_id``. Defaults to
            a lazily-constructed boto3-backed ``bedrock-agent`` caller.

    Returns:
        The started ingestion-job id, or ``None`` when skipped/failed.
    """
    resolved_kb = kb_id or os.environ.get("SEMANTIC_RAG_KB_ID", "")
    resolved_ds = data_source_id or os.environ.get("SEMANTIC_RAG_DATA_SOURCE_ID", "")
    if not resolved_kb or not resolved_ds:
        logger.warning(
            "KB ingestion skipped: SEMANTIC_RAG_KB_ID or "
            "SEMANTIC_RAG_DATA_SOURCE_ID not set"
        )
        return None

    def _default_start(kb: str, ds: str) -> str:
        """Start an ingestion job via a lazily-built bedrock-agent client."""
        import boto3  # Lazy import: never at module import time.

        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("bedrock-agent", region_name=region)
        resp = client.start_ingestion_job(knowledgeBaseId=kb, dataSourceId=ds)
        return resp["ingestionJob"]["ingestionJobId"]

    starter = start_ingestion or _default_start
    try:
        job_id = starter(resolved_kb, resolved_ds)
        logger.info("KB ingestion started: %s", job_id)
        return job_id
    except Exception as exc:  # noqa: BLE001 — ingestion trigger is best-effort
        logger.error("KB ingestion trigger failed (non-fatal): %s", exc)
        return None
