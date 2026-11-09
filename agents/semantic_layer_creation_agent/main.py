"""Hybrid semantic-layer creation agent runtime entrypoint.

The Hybrid creation agent builds BOTH artifacts sequentially with a two-phase
build status:

    buildingKB  ->  buildingVKG  ->  completed

The RICH prose (business purpose, synonyms, reference tables, sample SQL, sample
data, notes) is written to the Bedrock Knowledge Base by ``kb_builder``; the THIN
structural VKG (structure only, no prose) is emitted by ``vkg_builder`` and loaded
into Neptune. Status is reported to DynamoDB via ``progress.ProgressWriter``.

``_build_hybrid`` is the pure orchestrator: it sequences the two phases and the
status writes. It is fail-loud in intent but does NOT re-raise from inside itself
— it records a ``failed:<phase>`` status and returns a result dict describing the
outcome. Propagation (re-raise) happens at the entrypoint layer, which turns a
failed build into a DynamoDB ``failed`` status and re-raises so AgentCore records
the invocation failure. Keeping ``_build_hybrid`` non-raising lets the unit tests
drive it directly and assert the recorded status sequence without wrapping the
call in ``pytest.raises``.

Isolation note (``tests/unit/test_hybrid_isolation.py``): this module imports
ONLY from within ``agents.semantic_layer_creation_agent``, ``strands``, and
``bedrock_agentcore`` — never ``agents.shared`` or a sibling agent. All heavy
AWS / Bedrock / Strands calls live behind injectable seams so ``_build_hybrid``
(and the wiring around it) is unit-testable under the conftest stubs without a
live runtime.
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

try:
    from bedrock_agentcore import BedrockAgentCoreApp
except ImportError as exc:  # pragma: no cover — surfaced at container startup
    import sys

    print(f"STARTUP ERROR: failed to import BedrockAgentCoreApp: {exc}", flush=True)
    sys.exit(1)

# OTel baggage is used to tag the background thread's spans with the AgentCore
# runtimeSessionId; without it, Evaluations finds no spans for the session. Guarded
# because the conftest stubs import this module with no opentelemetry present.
try:
    from opentelemetry import baggage as _otel_baggage
    from opentelemetry import context as _otel_context
except ImportError:  # pragma: no cover — OTel absent under unit-test stubs
    _otel_baggage = None  # type: ignore[assignment]
    _otel_context = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Logging.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# AgentCore app instance — decorates ``invoke`` below.
app = BedrockAgentCoreApp()

region = os.getenv("AWS_REGION", "us-east-1")

# Phase status tokens — written verbatim to DynamoDB so the frontend renders the
# two-phase build progress.
STATUS_BUILDING_KB: str = "buildingKB"
STATUS_BUILDING_VKG: str = "buildingVKG"
STATUS_COMPLETED: str = "completed"

# A progress reporter records per-table build progress:
#   report(phase="kb"|"vkg", done=<tables finished>, total=<tables in layer>,
#          current=<table being processed>)
# It is a SEAM (never called directly) so the build stays unit-testable and a
# DynamoDB hiccup cannot reach the build thread.
ProgressReporter = Callable[..., None]

# Share of the progress bar each phase owns. The KB phase composes prose per
# table (the slow half) and the VKG phase reads the catalog per table; the last
# 5% covers the Neptune load + S3 save, which are not per-table.
_KB_PHASE_WEIGHT: int = 50
_VKG_PHASE_WEIGHT: int = 45


# ---------------------------------------------------------------------------
# Pure orchestrator — sequences the two build phases + status writes.
# ---------------------------------------------------------------------------
def _build_hybrid(
    *,
    id: str,
    write_status: Callable[[str], None],
    build_kb: Callable[[], None],
    build_vkg: Callable[[], None],
) -> Dict[str, Any]:
    """Drive the two-phase Hybrid build: KB, then VKG, then complete.

    Happy path status sequence: ``buildingKB`` -> ``buildingVKG`` ->
    ``completed``. On a KB failure the sequence is ``buildingKB`` ->
    ``failed:kb``; on a VKG failure it is ``buildingKB`` -> ``buildingVKG`` ->
    ``failed:vkg``.

    Fail-loud, but NON-RAISING here by contract: on a phase exception this
    function records the ``failed:<phase>`` status and returns a result dict
    describing the failure — it does NOT re-raise. The caller (the entrypoint)
    is responsible for propagating the failure (writing the terminal DynamoDB
    ``failed`` status and re-raising so AgentCore records the invocation error).
    This lets tests drive ``_build_hybrid`` directly and assert the recorded
    status sequence without wrapping the call in ``pytest.raises``.

    Args:
        id: The semantic-layer id being built (for logging/result context).
        write_status: Seam that records a build status (e.g. writes to DynamoDB).
        build_kb: Seam that builds + writes the rich KB documents. Called during
            the ``buildingKB`` phase.
        build_vkg: Seam that builds + loads the thin structural VKG. Called during
            the ``buildingVKG`` phase.

    Returns:
        ``{"status": "completed", "phase": "completed"}`` on success, or
        ``{"status": "failed", "phase": <phase>, "error": <str>}`` on a phase
        failure (``phase`` is ``"kb"`` or ``"vkg"``).
    """
    # --- Phase 1: rich KB documents --------------------------------------
    write_status(STATUS_BUILDING_KB)
    try:
        build_kb()
    except Exception as exc:  # noqa: BLE001 — record failed status, do not re-raise
        logger.error("[Hybrid] KB build failed for %s: %s", id, exc, exc_info=True)
        write_status("failed:kb")
        return {"status": "failed", "phase": "kb", "error": str(exc)}

    # --- Phase 2: thin structural VKG ------------------------------------
    write_status(STATUS_BUILDING_VKG)
    try:
        build_vkg()
    except Exception as exc:  # noqa: BLE001 — record failed status, do not re-raise
        logger.error("[Hybrid] VKG build failed for %s: %s", id, exc, exc_info=True)
        write_status("failed:vkg")
        return {"status": "failed", "phase": "vkg", "error": str(exc)}

    # --- Done ------------------------------------------------------------
    write_status(STATUS_COMPLETED)
    return {"status": "completed", "phase": "completed"}


# ---------------------------------------------------------------------------
# Boto session seam — injectable so the runtime can push scoped credentials and
# unit tests stay hermetic.
# ---------------------------------------------------------------------------
_boto_session = None


def set_boto_session(session) -> None:
    """Set the boto3 session used for all AWS API calls in this runtime.

    Args:
        session: A configured ``boto3.Session`` with the desired credentials.

    Returns:
        None.
    """
    global _boto_session
    _boto_session = session
    logger.info("Boto3 session set with region: %s", session.region_name)


def get_boto_session():
    """Return the configured boto3 session, creating a default one if unset.

    Returns:
        The active ``boto3.Session``.
    """
    global _boto_session
    if _boto_session is None:
        import boto3  # Lazy import: never at module import time.

        _boto_session = boto3.Session(region_name=region)
    return _boto_session


# ---------------------------------------------------------------------------
# DynamoDB config read (seam).
# ---------------------------------------------------------------------------
def _metadata_table():
    """Return the boto3 DynamoDB Table for the semantic-layer metadata catalog."""
    table_name = os.environ.get("ONTOLOGY_METADATA_TABLE", "semantic-layer-metadata")
    return get_boto_session().resource("dynamodb").Table(table_name)


def _version_num(version: str) -> int:
    """Parse the integer suffix from a version string like ``'v1'`` / ``'v10'``."""
    import re

    match = re.search(r"\d+", version or "v0")
    return int(match.group()) if match else 0


def _resolve_active_version(table, job_id: str) -> str:
    """Return the highest-numbered version sort-key for ``job_id`` (or ``'v1'``)."""
    from boto3.dynamodb.conditions import Key

    try:
        resp = table.query(
            KeyConditionExpression=Key("id").eq(job_id),
            ProjectionExpression="version",
        )
        items = resp.get("Items", [])
        if items:
            return max(items, key=lambda i: _version_num(i["version"]))["version"]
    except Exception as exc:  # noqa: BLE001 — fall back to v1 on any query error
        logger.warning("_resolve_active_version fallback to v1: %s", exc)
    return "v1"


def _read_config(id: str) -> Dict[str, Any]:
    """Read the active-version layer config item from DynamoDB.

    Args:
        id: The semantic-layer id (partition key).

    Returns:
        The active-version config item.

    Raises:
        ValueError: When no config record exists for ``id`` — fail loud rather
            than build against an empty config.
    """
    table = _metadata_table()
    active_version = _resolve_active_version(table, id)
    resp = table.get_item(
        Key={"id": id, "version": active_version}, ConsistentRead=True
    )
    if "Item" not in resp:
        raise ValueError(f"Hybrid creation config not found: {id}")
    return resp["Item"]


def _tables_from_config(config: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract the (database, table, catalogId) list from a layer config.

    Args:
        config: A metadata-layer config item.

    Returns:
        A list of ``{"database", "table", "catalogId"}`` dicts (only entries
        carrying both a database and a table name).
    """
    return [
        {
            "database": ds["databaseName"],
            "table": ds["tableName"],
            "catalogId": ds.get("catalogId", "AWSDataCatalog"),
        }
        for ds in config.get("dataSources", [])
        if ds.get("databaseName") and ds.get("tableName")
    ]


# ---------------------------------------------------------------------------
# Real build seams (KB write + VKG load). Kept module-level so the entrypoint
# wires them and unit tests can inspect/replace them.
# ---------------------------------------------------------------------------
def _make_build_kb(
    *,
    id: str,
    version: str,
    config: Dict[str, Any],
    tables: List[Dict[str, str]],
    report_progress: Optional[ProgressReporter] = None,
) -> Callable[[], None]:
    """Build the ``build_kb`` seam: write a rich KB document per table.

    The composed prose is produced by a Strands ``Agent`` over the KB system
    prompt; the resulting markdown is written to S3 by ``kb_builder``. Imports
    of ``strands`` / ``kb_builder`` are deferred into the returned closure so
    this module imports cleanly under the conftest stubs.

    Args:
        id: The semantic-layer id (also the KB document scope / DynamoDB pk).
        version: The active version sort-key scoping the KB documents.
        config: The layer config (domain context for the prose).
        tables: The (database, table, catalogId) dicts to document.
        report_progress: Optional per-table progress reporter (see
            :data:`ProgressReporter`); ``None`` disables progress reporting.

    Returns:
        A zero-arg callable that builds + writes every table's KB document,
        then triggers KB ingestion.
    """
    def _run() -> None:
        """Compose + write the rich KB document for every table, then ingest."""
        from . import kb_builder, query_prompts

        layer_tables = [t["table"] for t in tables]
        composer = _build_kb_composer()
        for index, t in enumerate(tables):
            schema_json, sample_json = _probe_table(
                database=t["database"], table=t["table"], catalog_id=t["catalogId"],
            )
            prompt = query_prompts.build_kb_prompt(
                database_name=t["database"],
                table_name=t["table"],
                catalog_id=t["catalogId"],
                schema_json=schema_json,
                sample_json=sample_json,
                use_cases_description=config.get("useCasesDescription", ""),
                data_sources_description=config.get("dataSourcesDescription", ""),
                layer_tables=layer_tables,
            )
            metadata_content = composer(prompt)
            kb_builder.write_kb_document(
                semantic_layer_id=id,
                semantic_layer_version=version,
                database_name=t["database"],
                table_name=t["table"],
                catalog_id=t["catalogId"],
                metadata_content=metadata_content,
            )
            if report_progress:
                report_progress(
                    phase="kb", done=index + 1, total=len(tables),
                    current=t["table"],
                )
        kb_builder.trigger_kb_ingestion()

    return _run


def _build_kb_composer() -> Callable[[str], str]:
    """Build the KB prose composer callable (Strands Agent over the KB prompt).

    Deferred ``strands`` import keeps this module importable under the conftest
    stubs. The model id is sourced from ``models.json`` (never a bare literal in
    code) so the manifest drift-detector stays green.

    Returns:
        A callable ``compose(prompt) -> markdown_text``.
    """
    from strands import Agent
    from strands.models import BedrockModel

    from . import query_prompts

    model = BedrockModel(model_id=_kb_model_id(), max_tokens=16000,
                         boto_session=get_boto_session())

    def _compose(prompt: str) -> str:
        """Compose one table's rich KB markdown from ``prompt``."""
        agent = Agent(model=model, system_prompt=query_prompts.KB_SYSTEM_PROMPT)
        response = agent(prompt)
        return response.message["content"][0]["text"]

    return _compose


def _kb_model_id() -> str:
    """Return the KB-composer model id declared first in ``models.json``.

    Reads the id from the manifest rather than hard-coding a literal, so the
    model-manifest drift-detector never flags this module.

    Returns:
        The foundation-model id string.

    Raises:
        ValueError: When ``models.json`` declares no foundation models — fail
            loud rather than call Bedrock with an empty model id.
    """
    import json
    from pathlib import Path

    manifest = Path(__file__).with_name("models.json")
    models = json.loads(manifest.read_text(encoding="utf-8")).get(
        "foundation_models", []
    )
    if not models:
        raise ValueError("models.json declares no foundation_models")
    return models[0]


def _glue_columns(
    *, database: str, table: str, catalog_id: str
) -> List[Dict[str, str]]:
    """Read a table's real column list (name/type/comment) from the Glue catalog.

    This is the SCHEMA ground truth for both artifacts: the KB prose prompt and
    the thin VKG's datatype properties. Reading the live catalog — rather than
    letting the model infer columns — is what keeps the VKG's ``mapsToColumn``
    mappings executable.

    Partition keys are appended because Glue reports them outside
    ``StorageDescriptor.Columns`` yet they are queryable columns like any other.

    Parameters:
        database: Glue database name.
        table: Table name within that database.
        catalog_id: Catalog identifier. ``AWSDataCatalog``/``AwsDataCatalog``
            means the account's default catalog and is NOT passed to Glue;
            anything else (e.g. an S3 Tables federated catalog) is passed as
            ``CatalogId``.
    Returns:
        list[dict]: ``[{"name","type","comment"}, ...]`` in catalog order.
    Raises:
        RuntimeError: Glue returned no columns for the table. A column-less
            thin VKG cannot ground anything, so this fails loudly rather than
            producing an empty graph that looks like a successful build.
    """
    glue = get_boto_session().client("glue")
    kwargs: Dict[str, Any] = {"DatabaseName": database, "Name": table}
    # Federated / non-default catalogs must be named explicitly; the default
    # catalog is implied by the caller's account and rejects the literal name.
    if catalog_id and catalog_id not in ("AWSDataCatalog", "AwsDataCatalog"):
        kwargs["CatalogId"] = catalog_id
    tbl = glue.get_table(**kwargs)["Table"]
    columns: List[Dict[str, str]] = [
        {
            "name": col["Name"],
            "type": col.get("Type", "string"),
            "comment": col.get("Comment", ""),
        }
        for col in tbl.get("StorageDescriptor", {}).get("Columns", [])
    ]
    for partition_key in tbl.get("PartitionKeys", []) or []:
        columns.append(
            {
                "name": partition_key["Name"],
                "type": partition_key.get("Type", "string"),
                "comment": partition_key.get("Comment", ""),
            }
        )
    if not columns:
        raise RuntimeError(
            f"Glue returned no columns for {database}.{table} "
            f"(catalog={catalog_id!r}). Refusing to build a column-less thin VKG: "
            f"it would have no datatype properties or mapsToColumn mappings and "
            f"could not ground any query. Check the creation role's Lake "
            f"Formation / Glue grants."
        )
    return columns


def _probe_table(*, database: str, table: str, catalog_id: str) -> tuple[str, str]:
    """Probe a table's schema + sample values for the KB prompt (best-effort).

    Reads the real column list from Glue, then samples distinct values for each
    column via the within-package ``athena_probe`` seam so the composed prose can
    describe actual enum values rather than guessing.

    FAIL-SOFT BY DESIGN: any probe error yields empty JSON so the composer still
    writes a document. This is deliberate and differs from the VKG path — the KB
    is advisory context for prose, so one table's missing sample must not abort a
    whole corpus, whereas a missing VKG column WOULD break grounding and does
    fail loudly (see :func:`_glue_columns`).

    Args:
        database: Glue/Athena database name.
        table: Table name.
        catalog_id: Catalog identifier.

    Returns:
        ``(schema_json, sample_json)`` — JSON strings (possibly ``"{}"``).
    """
    import json

    from . import athena_probe

    logger.info("KB probe for %s.%s (catalog=%s)", database, table, catalog_id)
    try:
        columns = _glue_columns(
            database=database, table=table, catalog_id=catalog_id
        )
    except Exception as exc:  # noqa: BLE001 — KB prose is advisory; see docstring
        logger.warning(
            "KB schema probe failed for %s.%s (%s) — composing without schema",
            database, table, exc,
        )
        return json.dumps({}), json.dumps({})

    schema_json = json.dumps(
        {
            "database_name": database,
            "table_name": table,
            "columns": columns,
            "total_columns": len(columns),
        }
    )

    output_location = os.environ.get("ATHENA_OUTPUT_LOCATION") or None
    workgroup = os.environ.get("ATHENA_WORKGROUP", "primary")
    # Athena's catalog name differs from Glue's CatalogId: the default catalog IS
    # addressed as "AWSDataCatalog" here.
    athena_catalog = catalog_id or "AWSDataCatalog"
    samples: Dict[str, Any] = {}
    for column in columns:
        try:
            samples[column["name"]] = athena_probe.probe_column_values(
                table=table,
                column=column["name"],
                database=database,
                catalog=athena_catalog,
                workgroup=workgroup,
                output_location=output_location,
            )
        except Exception as exc:  # noqa: BLE001 — per-column fail-soft
            logger.warning(
                "value probe failed for %s.%s.%s (%s) — omitting samples",
                database, table, column["name"], exc,
            )
    return schema_json, json.dumps(samples)


def vkg_graph_iri(*, namespace: str) -> str:
    """Return the named-graph IRI the thin VKG for ``namespace`` is loaded into.

    ONE graph per layer, with the layer id embedded in the IRI. Both properties
    matter: the query side derives the same IRI to scope its CONSTRUCT (see
    ``semantic_layer_query_agent/neptune_gateway.py::vkg_graph_iri``), and the
    gateway lambda's ``_resolve_graph_uri`` finds a layer's graph by substring-
    matching the id against graph URIs.

    Parameters:
        namespace: The semantic-layer namespace / layer id.
    Returns:
        str: The named-graph IRI (no angle brackets).
    """
    return f"https://semantic-layer.aws/vkg/{namespace}"


def _verify_target_keys(
    *,
    tables: List[Dict[str, str]],
    key_candidates: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Measure which referenced columns are REAL single-column keys of their table.

    Ontop unifies an FK's object template with the target class's SUBJECT template
    structurally, so an inferred edge only becomes a real SQL join when the target
    class declares the referenced column as its key. Declaring a key also licences
    Ontop to drop de-duplication, so it is asserted ONLY from measurement — never
    from the column's name.

    FAIL-SOFT BY DESIGN, and deliberately so: a probe that errors means the key is
    UNKNOWN, which leaves row identity on the consumer's name heuristic (loudly
    logged there). That is a degraded layer, not a wrong one — whereas failing the
    whole build over an unmeasurable table would throw away a graph that is
    otherwise correct.

    Parameters:
        tables: The layer's ``{"database", "table", "catalogId"}`` descriptors.
        key_candidates: Mapping of table name -> referenced column names to
            measure (from ``fk_inference.key_columns_by_table``).
    Returns:
        dict[str, list[str]]: table name -> its verified key columns (only tables
            with exactly one verified column are included; a table where several
            candidates are unique cannot have its identity chosen by measurement
            alone, so it is left to the consumer's heuristic).
    """
    from . import athena_probe

    coordinates = {t["table"]: t for t in tables}
    workgroup = os.environ.get("ATHENA_WORKGROUP", "primary")
    output_location = os.environ.get("ATHENA_OUTPUT_LOCATION") or None
    verified: Dict[str, List[str]] = {}

    for table, candidates in key_candidates.items():
        coordinate = coordinates.get(table)
        if not coordinate:
            continue
        try:
            unique = athena_probe.probe_unique_columns(
                table=table,
                columns=candidates,
                database=coordinate["database"],
                # Athena addresses the default catalog as "AWSDataCatalog"
                # (unlike Glue's CatalogId) — mirror _probe_table.
                catalog=coordinate["catalogId"] or "AWSDataCatalog",
                workgroup=workgroup,
                output_location=output_location,
            )
        except Exception as exc:  # noqa: BLE001 — unmeasured key; see docstring
            logger.warning(
                "key probe failed for %s.%s (%s) — declaring NO key, so row "
                "identity for this class stays a name guess",
                coordinate["database"], table, exc,
            )
            continue
        if len(unique) == 1:
            verified[table] = unique
            logger.info("verified key %s.%s", table, unique[0])
        else:
            logger.info(
                "no single verified key for %s (candidates=%s, unique=%s) — "
                "declaring none",
                table, candidates, unique,
            )
    return verified


def _harvest_kb_descriptions(
    *,
    id: str,
    version: str,
    tables: List[Dict[str, str]],
) -> Dict[str, Dict[str, object]]:
    """Read each table's KB document and harvest its short descriptions.

    The documentation phase already wrote a one-line description for every column
    into the KB markdown; this lifts those lines (and the table's Overview
    paragraph) so the structural phase can emit them as ``rdfs:comment``. Without
    it the graph carries no descriptive text at all and the admin viewer's
    Description column renders an em-dash for every property.

    FAIL-SOFT BY DESIGN, per table: a missing or unparseable KB document yields no
    descriptions for that table and logs a WARNING naming it. Descriptions are
    additive — they change nothing about whether the graph grounds a query — so
    aborting a correct structural build over them would trade a cosmetic gap for a
    failed layer. Contrast ``_glue_columns``, which DOES fail loudly because a
    missing column breaks grounding.

    Args:
        id: The semantic-layer id scoping the KB documents.
        version: The active version sort-key scoping the KB documents.
        tables: The (database, table, catalogId) dicts being built.

    Returns:
        dict: Table name -> ``{"table": str, "columns": dict[str, str]}`` as
            returned by ``kb_descriptions.parse_kb_document``. Tables whose
            document could not be read are absent from the mapping.
    """
    from . import kb_builder, kb_descriptions

    harvested: Dict[str, Dict[str, object]] = {}
    for t in tables:
        try:
            markdown = kb_builder.read_kb_document(
                semantic_layer_id=id,
                semantic_layer_version=version,
                database_name=t["database"],
                table_name=t["table"],
                catalog_id=t["catalogId"],
            )
        except Exception as exc:  # noqa: BLE001 — descriptions are additive
            logger.warning(
                "no KB document to harvest descriptions from for %s.%s (%s) — "
                "its properties will carry no rdfs:comment",
                t["database"], t["table"], exc,
            )
            continue
        parsed = kb_descriptions.parse_kb_document(markdown)
        columns = parsed["columns"]
        if not columns:
            logger.warning(
                "KB document for %s.%s has no parseable '## Columns' description "
                "table — its properties will carry no rdfs:comment",
                t["database"], t["table"],
            )
        harvested[t["table"]] = parsed
    return harvested


def _edge_comment(*, edge: Dict[str, str], verified: bool) -> str:
    """Describe one inferred relationship edge in a single line.

    Deterministic (no model): it states the physical columns the edge was inferred
    from, which rule inferred it, and whether the target key was MEASURED unique.
    That last part is the one thing a reviewer cannot see anywhere in the viewer
    otherwise, and it is what separates a join Ontop can trust from a guess.

    Args:
        edge: An ``fk_inference.infer_joins`` edge dict.
        verified: Whether the target column was measured unique.

    Returns:
        str: The description line.
    """
    verdict = (
        "target key measured unique"
        if verified
        else "target key NOT measured unique"
    )
    return (
        f"Inferred foreign key: {edge['source_table']}.{edge['source_column']} "
        f"references {edge['target_table']}.{edge['target_column']} "
        f"(rule: {edge.get('rule', 'unknown')}; {verdict})."
    )


def _make_build_vkg(
    *,
    id: str,
    version: str,
    config: Dict[str, Any],
    tables: List[Dict[str, str]],
    report_progress: Optional[ProgressReporter] = None,
) -> Callable[[], None]:
    """Build the ``build_vkg`` seam: emit the thin VKG + load it into Neptune.

    Emits structural N-Quads per table via ``vkg_builder`` using the REAL Glue
    columns, infers the layer's relationship edges from those columns, verifies
    the referenced target columns by measurement, then loads everything into
    Neptune through the gateway. Each class and property also gets ONE
    ``rdfs:comment`` line, harvested from the KB documents the documentation phase
    wrote (see :func:`_harvest_kb_descriptions`); the prose sections themselves
    stay in the KB. The Neptune load and the builder live behind deferred imports
    so this module imports cleanly under the conftest stubs.

    Args:
        id: The semantic-layer id (namespace / vocab scope).
        version: The active version sort-key scoping the KB documents to harvest
            descriptions from.
        config: The layer config (vocab / graph namespace).
        tables: The (database, table, catalogId) dicts to emit structure for.
        report_progress: Optional per-table progress reporter (see
            :data:`ProgressReporter`); ``None`` disables progress reporting.

    Returns:
        A zero-arg callable that builds the thin VKG and loads it into Neptune.
    """
    def _run() -> None:
        """Emit the thin structural VKG for every table and load it into Neptune."""
        from . import fk_inference, vkg_builder
        from .ontop_mappings import (
            VIRTUAL_KG_VOCAB,
        )

        # The CLASS/property IRI namespace. Defaults to the shared VKG vocabulary
        # the query side reads mapping predicates against, so a layer that does
        # not override it stays groundable.
        vocab = (
            config.get("vkgVocab")
            or os.environ.get("VKG_VOCAB")
            or VIRTUAL_KG_VOCAB
        )
        graph_iri = vkg_graph_iri(namespace=id)

        # --- Pass 1: the live schema for EVERY table --------------------------
        # Read all columns up front because relationship inference is a
        # CROSS-table question: an edge can only be inferred once the referenced
        # table's columns are known, so it cannot be done inside a per-table loop.
        columns_by_table: Dict[str, List[Dict[str, str]]] = {}
        for index, t in enumerate(tables):
            # The class IRI is derived from the table name; the PROSE SECTIONS stay
            # in the KB (only a one-line description is harvested back below).
            # Columns DO come from the live Glue catalog — without them the graph
            # would carry classes but no datatype properties or mapsToColumn
            # mappings, and could not ground any query.
            columns_by_table[t["table"]] = _glue_columns(
                database=t["database"], table=t["table"], catalog_id=t["catalogId"],
            )
            if report_progress:
                report_progress(
                    phase="vkg", done=index + 1, total=len(tables),
                    current=t["table"],
                )

        # --- Relationship inference (deterministic, no model) ----------------
        # Without this the graph carries classes and columns but not one
        # owl:ObjectProperty, which is what made the viewer report "no
        # relationships" and left the query side with no declared join edges.
        edges = fk_inference.infer_joins(
            {
                table: [str(column["name"]) for column in columns]
                for table, columns in columns_by_table.items()
            }
        )
        logger.info("[Hybrid] inferred %d relationship edge(s) for %s", len(edges), id)

        # A verified key on the TARGET class is what turns an inferred edge into a
        # real SQL join (Ontop unifies FK object and subject templates
        # structurally), so the referenced columns are measured before emitting.
        verified_keys = _verify_target_keys(
            tables=tables,
            key_candidates=fk_inference.key_columns_by_table(edges),
        )

        joins_by_table: Dict[str, List[Dict[str, str]]] = {}
        for edge in edges:
            source = edge["source_table"]
            # Every IRI segment goes through iri_local_name: a table or inferred
            # property name carrying a space (or any other character SPARQL's
            # IRIREF forbids) produces an IRI that N-Quads validation accepts and
            # Neptune's INSERT DATA rejects. target_column keeps the RAW names —
            # it is the physical coordinate the OBDA mapping reads.
            source_local = vkg_builder.iri_local_name(source)
            joins_by_table.setdefault(source, []).append(
                {
                    "property": (
                        f"{vocab}{source_local}/"
                        f"{vkg_builder.iri_local_name(edge['property_name'])}"
                    ),
                    # The FK column lives on the SOURCE table: that is the column
                    # the OBDA template reads to build the target's subject IRI.
                    "target_column": f"{source}.{edge['source_column']}",
                    # rdfs:range must be the target CLASS IRI, so it has to be
                    # minted exactly the way class_iri is below — otherwise the
                    # edge points at an IRI no class was ever emitted for.
                    "range": f"{vocab}{vkg_builder.iri_local_name(edge['target_table'])}",
                    "label": edge["property_name"],
                    "comment": _edge_comment(
                        edge=edge,
                        verified=edge["target_column"]
                        in (verified_keys.get(edge["target_table"]) or []),
                    ),
                }
            )

        # --- Descriptions: harvested from this layer's KB documents -----------
        descriptions = _harvest_kb_descriptions(
            id=id, version=version, tables=tables
        )

        # --- Pass 2: emit ----------------------------------------------------
        nquads_parts: List[str] = []
        for t in tables:
            # Sanitized for the same reason the property IRIs are (see the join
            # loop above); `table=` below still gets the RAW name, which is what
            # the mapsToTable literal and the generated SQL must use.
            class_iri = f"{vocab}{vkg_builder.iri_local_name(t['table'])}"
            described = descriptions.get(t["table"], {})
            nquads_parts.append(
                vkg_builder.build_thin_vkg_nquads(
                    table=t["table"],
                    columns=columns_by_table[t["table"]],
                    class_iri=class_iri,
                    graph_iri=graph_iri,
                    joins=joins_by_table.get(t["table"]),
                    key_columns=verified_keys.get(t["table"]),
                    class_comment=str(described.get("table") or "") or None,
                    column_comments=described.get("columns") or None,
                )
            )
        nquads = "\n".join(nquads_parts)
        _load_vkg_into_neptune(namespace=id, nquads=nquads)
        # The Metadata tab reads the layer's artifact from S3 via metadataPath;
        # without this a completed Hybrid layer has nothing to show there and the
        # tab renders a bare "An error occurred processing your request."
        metadata_path = _save_vkg_to_s3(id=id, nquads=nquads)
        # Stamp ONLY after a successful load: the query side treats these fields
        # as proof a structural graph exists, so stamping earlier would make a
        # failed build look ready and turn every query into a silent degrade.
        _stamp_vkg_built(id=id, graph_iri=graph_iri, metadata_path=metadata_path)

    return _run


def _save_vkg_to_s3(*, id: str, nquads: str) -> str:
    """Persist the thin VKG N-Quads to the artifacts bucket and return its URI.

    Neptune is the query path's source of truth, so for a long time the hybrid
    build wrote nothing to S3. But the REST API serves the layer's Metadata tab
    from S3 (``ontology_service.get_metadata_content`` reads ``metadataPath``),
    and with no object stored that endpoint raises and the tab renders a bare
    "An error occurred processing your request." Writing the same N-Quads that
    were loaded into Neptune also gives the layer a reviewable, diffable artifact.

    The key matches the non-hybrid layout (``ontologies/{id}/ontology.nq``) so
    every reader of a layer's artifact — Metadata tab, delta preview, manual
    inspection — finds a hybrid layer's graph exactly where it finds any other's.

    Parameters:
        id: The semantic-layer id (S3 prefix).
        nquads: The thin structural N-Quads text that was loaded into Neptune.
    Returns:
        str: The ``s3://`` URI of the written object.
    Raises:
        RuntimeError: ``ARTIFACTS_BUCKET`` is unset, or the write failed. Fail
            loud: a completed layer whose artifact is missing looks healthy in the
            catalog while every read of its metadata errors.
    """
    bucket = os.environ.get("ARTIFACTS_BUCKET", "")
    if not bucket:
        raise RuntimeError(
            "ARTIFACTS_BUCKET is not configured — cannot persist the thin VKG "
            "artifact, and the layer's Metadata tab would error on every read"
        )
    key = f"ontologies/{id}/ontology.nq"
    try:
        get_boto_session().client("s3").put_object(
            Bucket=bucket,
            Key=key,
            Body=nquads.encode("utf-8"),
            ContentType="application/n-quads",
        )
    except Exception as exc:  # noqa: BLE001 — re-raised below with context
        raise RuntimeError(
            f"Loaded the thin VKG for {id} but could not write s3://{bucket}/{key}: "
            f"{exc}. The layer's Metadata tab reads that object, so it would error "
            f"on every view — failing the build instead."
        ) from exc
    s3_uri = f"s3://{bucket}/{key}"
    logger.info("Saved thin VKG artifact for %s to %s", id, s3_uri)
    return s3_uri


def _stamp_vkg_built(
    *, id: str, graph_iri: str, metadata_path: Optional[str] = None
) -> None:
    """Record on the layer config that its thin VKG is materialized in Neptune.

    ``semantic_layer_query_agent::_vkg_exists`` gates hybrid answering on
    ``vkgBuilt``/``vkgGraphId``, so without this stamp every hybrid query would
    degrade to ``hybrid_artifact_missing`` even after a perfect build.
    ``metadataPath`` is the REST API's pointer to the S3 artifact behind the
    layer's Metadata tab.

    Parameters:
        id: The semantic-layer id (partition key).
        graph_iri: The named-graph IRI the VKG was loaded into.
        metadata_path: The ``s3://`` URI of the persisted N-Quads artifact. When
            omitted no ``metadataPath`` is written.
    Returns:
        None.
    Raises:
        RuntimeError: The stamp could not be written. This is NOT best-effort:
            an unstamped layer is unusable by the query side, so a build that
            cannot stamp must fail rather than report a completed hybrid layer.
    """
    table = _metadata_table()
    version = _resolve_active_version(table, id)
    values: Dict[str, Any] = {":built": True, ":graph": graph_iri}
    expression = "SET vkgBuilt = :built, vkgGraphId = :graph"
    if metadata_path:
        expression += ", metadataPath = :path"
        values[":path"] = metadata_path
    try:
        table.update_item(
            Key={"id": id, "version": version},
            UpdateExpression=expression,
            ExpressionAttributeValues=values,
        )
    except Exception as exc:  # noqa: BLE001 — re-raised below with context
        raise RuntimeError(
            f"Loaded the thin VKG for {id} but could not stamp vkgBuilt/vkgGraphId "
            f"(version={version}): {exc}. The query agent gates on those fields, so "
            f"the layer would be unusable — failing the build instead."
        ) from exc
    logger.info("Stamped vkgBuilt=true vkgGraphId=%s on %s (%s)", graph_iri, id, version)


def _load_vkg_into_neptune(*, namespace: str, nquads: str) -> None:
    """Load the thin-VKG N-Quads into Neptune via the AgentCore Neptune Gateway.

    This runtime has no direct Neptune access (no VPC attachment, no SigV4
    endpoint), so the load goes through the gateway's ``persist_to_neptune`` tool,
    which groups the quads by named graph and issues ``INSERT DATA`` per graph.

    Because that is an INSERT and not a replace, the layer's previous graph is
    dropped first via ``delete_graph``; otherwise every triple a rebuild no longer
    emits stays behind and the layer accumulates superseded relationships.

    Args:
        namespace: The semantic-layer namespace the graph belongs to.
        nquads: The thin structural N-Quads text to load.

    Returns:
        None.

    Raises:
        RuntimeError: No gateway is configured, or the clear or the load failed.
            Fail loud in all cases so a build can never report a completed hybrid
            layer without a materialized structural graph — or with one that mixes
            two builds' triples.
    """
    gateway_url = os.environ.get("NEPTUNE_GATEWAY_URL", "")
    if not gateway_url:
        raise RuntimeError(
            "NEPTUNE_GATEWAY_URL is not configured — cannot load the thin VKG; "
            "refusing to report a completed hybrid build without a materialized "
            "structural graph"
        )
    # Deferred import: neptune_load pulls in strands/mcp lazily, and this keeps
    # main.py importable under the conftest stubs.
    from .neptune_load import (
        NeptuneLoadClient,
        build_gateway_client,
    )

    logger.info(
        "Loading thin VKG for namespace %s (%d bytes) via gateway",
        namespace, len(nquads.encode("utf-8")),
    )
    mcp_client = build_gateway_client(gateway_url=gateway_url, region=region)
    with mcp_client:
        client = NeptuneLoadClient(mcp_client=mcp_client)
        # Drop the previous build's graph FIRST: the persist tool only ever
        # INSERTs, so without this a rebuild leaves every superseded triple in
        # place (see NeptuneLoadClient.clear).
        client.clear(ontology_id=namespace)
        client.persist(nquads=nquads)


# ---------------------------------------------------------------------------
# DynamoDB terminal-status writer (entrypoint layer).
# ---------------------------------------------------------------------------
def _write_terminal_status(
    *,
    id: str,
    version: str,
    status: str,
    remove: Optional[List[str]] = None,
    **extra,
) -> None:
    """Write a terminal build status (+ timestamp / extra fields) to DynamoDB.

    Best-effort: a DynamoDB error is logged and swallowed so a status-write
    hiccup never masks the real build outcome.

    Args:
        id: The semantic-layer id (partition key).
        version: The version sort-key to target.
        status: The terminal status value (e.g. ``'completed'`` / ``'failed'``).
        remove: Attribute names to DELETE in the same update. A rebuild after a
            failed attempt must clear that attempt's ``error`` / ``failedAt``,
            otherwise they sit on a ``completed`` record and the UI keeps
            rendering the old failure alongside the success.
        **extra: Additional attributes to persist (e.g. ``error``, ``summary``).
    """
    try:
        table = _metadata_table()
        data = {
            "status": status,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        names = {f"#{k}": k for k in data}
        update_expr = "SET " + ", ".join(f"#{k} = :{k}" for k in data)
        if remove:
            # Alias every removed name too — `error` and `status` are DynamoDB
            # reserved words and would fail the expression unaliased.
            for index, key in enumerate(remove):
                names[f"#rm{index}"] = key
            update_expr += " REMOVE " + ", ".join(
                f"#rm{index}" for index, _key in enumerate(remove)
            )
        table.update_item(
            Key={"id": id, "version": version},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={f":{k}": v for k, v in data.items()},
        )
        logger.info("Terminal status: %s (version=%s) -> %s", id, version, status)
    except Exception as exc:  # noqa: BLE001 — terminal status write is best-effort
        logger.error("_write_terminal_status failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# AgentCore entrypoint.
# ---------------------------------------------------------------------------
def _run_build(*, id: str, version: str, config: Dict[str, Any],
               tables: List[Dict[str, str]]) -> Dict[str, Any]:
    """Wire the real build seams, run the build, and write the terminal status.

    Extracted from :func:`invoke` so it can run on a background thread: the whole
    build (KB prose per table + the thin-VKG load) takes far longer than AgentCore
    allows a single synchronous invocation to occupy, so the entrypoint hands off
    to this function and returns immediately.

    Args:
        id: The semantic-layer id (DynamoDB partition key).
        version: The active version sort-key.
        config: The layer's DynamoDB config item.
        tables: The table descriptors parsed from ``config['dataSources']``.

    Returns:
        A dict describing the completed build.

    Raises:
        RuntimeError: When the build failed. Raised only AFTER the terminal
            ``failed`` status is recorded, so the DynamoDB record always reflects
            the real outcome (the fail-loud propagation ``_build_hybrid`` defers).
    """
    from .progress import ProgressWriter

    progress = ProgressWriter(job_id=id, version=version)
    total = len(tables)

    def report(*, phase: str, done: int, total: int, current: str) -> None:
        """Persist per-table progress so the frontend can render a live build.

        The two phases share one 0-100 bar: the KB phase fills the first
        ``_KB_PHASE_WEIGHT`` percent and the VKG phase the next
        ``_VKG_PHASE_WEIGHT``, leaving the remainder for the terminal write. The
        build's OWN status token is written by ``_build_hybrid``; this reporter
        re-sends it so a single update carries both the phase and its counters.

        Args:
            phase: ``'kb'`` or ``'vkg'`` — which build phase is reporting.
            done: Tables finished in this phase so far.
            total: Tables in this phase overall.
            current: The table just finished.
        Returns:
            None (the underlying write is best-effort by design).
        """
        share = done / total if total else 1.0
        if phase == "kb":
            percent = round(_KB_PHASE_WEIGHT * share)
            status = "buildingKB"
        else:
            percent = _KB_PHASE_WEIGHT + round(_VKG_PHASE_WEIGHT * share)
            status = "buildingVKG"
        progress.write(
            status,
            fields={
                "totalTables": total,
                "tablesProcessed": done,
                "currentTable": current,
                "progressPercent": percent,
            },
        )

    # Seed the counters BEFORE the first phase: the frontend polls immediately
    # after kicking off the build, and a record with no totalTables renders
    # "0 tables" for however long the first table takes.
    progress.write(
        "buildingKB",
        fields={"totalTables": total, "tablesProcessed": 0, "progressPercent": 0},
    )

    build_kb = _make_build_kb(
        id=id, version=version, config=config, tables=tables,
        report_progress=report,
    )
    build_vkg = _make_build_vkg(
        id=id, version=version, config=config, tables=tables,
        report_progress=report,
    )

    result = _build_hybrid(
        id=id,
        write_status=progress.write,
        build_kb=build_kb,
        build_vkg=build_vkg,
    )

    if result.get("status") == "failed":
        phase = result.get("phase", "unknown")
        error = result.get("error", "unknown error")
        # Record the terminal DynamoDB failed status, then RE-RAISE so AgentCore
        # records the invocation failure (the propagation _build_hybrid defers).
        _write_terminal_status(
            id=id, version=version, status="failed",
            failedAt=datetime.now(timezone.utc).isoformat(),
            error=f"{phase}: {error}",
        )
        raise RuntimeError(f"Hybrid build failed in phase {phase}: {error}")

    _write_terminal_status(
        id=id, version=version, status="completed",
        completedAt=datetime.now(timezone.utc).isoformat(),
        totalTables=total,
        tablesProcessed=total,
        progressPercent=100,
        # A rebuild that succeeds must clear the previous attempt's failure, or
        # the completion screen shows the stale error next to the success.
        remove=["error", "failedAt"],
    )
    return {"status": "completed", "id": id, "version": version,
            "tableCount": len(tables)}


@app.entrypoint
def invoke(payload, context):
    """Entrypoint for the Hybrid creation agent — starts the build in background.

    A 40-table build spends roughly half a minute per table on the KB-prose phase
    alone, which is well past the window AgentCore keeps a synchronous invocation
    alive: doing the work inline gets the container REAPED mid-build with no
    traceback and no terminal DynamoDB status, then crash-looped. So this mirrors
    ``agents/ontology_agent/main.py``: register an async task (which keeps the
    runtime's health reporting busy instead of idle), run :func:`_run_build` on a
    daemon thread, and return ``processing`` within a few seconds. Callers poll the
    DynamoDB status record for the real outcome.

    Args:
        payload: The invocation payload — requires ``id`` (the semantic-layer id).
        context: The AgentCore invocation context.

    Returns:
        A dict with ``status='processing'`` and the async ``task_id``, or a dict
        with an ``error`` key when the build could not be STARTED.
    """
    id = payload.get("id")
    if not id:
        raise ValueError("id required in payload")

    session_id = (
        context.session_id if hasattr(context, "session_id") else str(uuid.uuid4())
    )
    # set_baggage returns a NEW context rather than mutating the active one, so it
    # must be attached here — copy_context() below then carries it into the thread,
    # which is what tags the build's spans with this runtimeSessionId.
    if _otel_baggage and _otel_context:
        _otel_context.attach(_otel_baggage.set_baggage("session.id", session_id))
    logger.info("[Hybrid] Starting creation for %s (session=%s)", id, session_id)

    try:
        config = _read_config(id)
        version = _resolve_active_version(_metadata_table(), id)
        tables = _tables_from_config(config)
    except Exception as exc:  # noqa: BLE001 — cannot start: record and report
        logger.error("[Hybrid] Failed to start build for %s: %s", id, exc,
                     exc_info=True)
        _write_terminal_status(
            id=id, version="v1", status="failed",
            failedAt=datetime.now(timezone.utc).isoformat(),
            error=f"startup: {exc}",
        )
        return {"error": str(exc), "id": id}

    logger.info("[Hybrid] %d table(s) to build for %s", len(tables), id)

    # Tell AgentCore an async task is in flight. Without this the runtime reports
    # itself idle once the entrypoint returns and is eligible for reaping while the
    # background thread is still building.
    task_id = app.add_async_task("hybrid_build", {"id": id})

    def background_work() -> None:
        """Run the build off the request thread, always clearing the async task.

        Returns:
            None. Failures are already recorded in DynamoDB by :func:`_run_build`;
            they are logged here because nothing can re-raise them to a caller.
        """
        try:
            _run_build(id=id, version=version, config=config, tables=tables)
        except Exception as exc:  # noqa: BLE001 — no caller left to propagate to
            logger.error("[Hybrid] Build failed for %s: %s", id, exc, exc_info=True)
        finally:
            app.complete_async_task(task_id)

    threading.Thread(
        target=contextvars.copy_context().run, args=(background_work,), daemon=True
    ).start()

    return {
        "status": "processing",
        "message": "Hybrid semantic-layer build started in background",
        "id": id,
        "version": version,
        "tableCount": len(tables),
        "task_id": task_id,
    }


if __name__ == "__main__":  # pragma: no cover — container entrypoint
    logger.info("Starting Hybrid Semantic Layer Creation Agent")
    app.run()
