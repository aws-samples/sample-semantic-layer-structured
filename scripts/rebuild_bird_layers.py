#!/usr/bin/env python3
"""Rebuild the BIRD VKG semantic layers in parallel, one thread per database.

Notebook 11 builds every BIRD layer in ONE sequential cell (a 14-hour cell
timeout), which is the right shape for an evaluated build — its
BatchEvaluationRunner scores each build's spans. It is the wrong shape for a
mechanical rebuild forced by a code change: the eleven builds are independent,
the ontology agent runs each in its own background thread on its own runtime
session, and nothing here needs the OntologyQuality judge.

This driver therefore replicates only nb11's build path — seed the DynamoDB v1
config under the deterministic ``vkg-<namespace>`` id, invoke the ontology
runtime, poll DynamoDB to a terminal status — and runs the databases
concurrently. It reuses the notebook's own conventions deliberately (the same
manifest, the same layer ids, the same data-source dicts) so a layer rebuilt here
is addressable by notebook 12's evaluation exactly as one rebuilt by nb11.

Verification is stricter than nb11's, because the reason for a rebuild is usually
a fix that must be OBSERVABLE: after a build completes, the layer's ontology is
read back out of Neptune through the gateway and the ``vkg:hasPrimaryKey`` triples
are counted. A build that completes without carrying the fix is reported, not
silently accepted.

Usage:
    python scripts/rebuild_bird_layers.py                      # all manifest DBs
    python scripts/rebuild_bird_layers.py --dbs bird_toxicology,bird_financial
    python scripts/rebuild_bird_layers.py --concurrency 4
    python scripts/rebuild_bird_layers.py --verify-only        # no builds, just report
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmarks.ingest_common import Manifest  # noqa: E402

PROJECT_NAME = "semantic-layer"
OAUTH_SCOPE = "semantic-layer-mcp/invoke"

# The Cognito hosted-UI domain fronts /oauth2/token behind a WAF that rejects
# urllib's default User-Agent with a bare 403. Same string nb11 sends.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Poll budget, matching nb11's reasoning: the incremental phase costs ~150s per
# table and the refinement phase is roughly constant, and the two ADD.
POLL_FLOOR_SECS = int(os.environ.get("BUILD_POLL_FLOOR_SECS", "3600"))
POLL_PER_TABLE_SECS = 150
POLL_INTERVAL_SECS = 30

# Generic VKG use-cases text, copied from nb11 so the agent receives the same
# instructions it did on the build being replaced (a different prompt would make
# any before/after measurement a comparison of two changes, not one).
VKG_USE_CASES = (
    "Build an OWL ontology (virtual knowledge graph) over the ingested benchmark "
    "database so analysts can ask graph-grounded natural-language questions. Model "
    "one owl:Class per table, an owl:DatatypeProperty per column, and foreign-key "
    "relationships as owl:ObjectProperty with rdfs:domain/rdfs:range, each with a "
    "business-meaningful rdfs:comment."
)

_print_lock = threading.Lock()


def log(message: str) -> None:
    """Print a line without interleaving output from concurrent build threads.

    Args:
        message: The line to print.
    """
    with _print_lock:
        print(message, flush=True)


def get_stack_outputs(*, cfn_client: Any, stack_suffix: str) -> Dict[str, str]:
    """Read a CloudFormation stack's outputs, trying both stack-name conventions.

    Args:
        cfn_client: A boto3 CloudFormation client.
        stack_suffix: The stack suffix, e.g. "agentcore" or "auth".

    Returns:
        Mapping of OutputKey -> OutputValue.

    Raises:
        RuntimeError: Neither stack-name variant exists.
    """
    for stack_name in (f"{PROJECT_NAME}-{stack_suffix}", f"{PROJECT_NAME}-dev-{stack_suffix}"):
        try:
            stacks = cfn_client.describe_stacks(StackName=stack_name)["Stacks"]
        except Exception:  # noqa: BLE001 — a missing stack is expected for one variant
            continue
        return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    raise RuntimeError(f"Stack not found for suffix '{stack_suffix}' (project {PROJECT_NAME})")


def get_m2m_creds(*, session: boto3.Session) -> Tuple[str, str, str]:
    """Return (token_endpoint, client_id, client_secret) for the M2M OAuth client.

    Args:
        session: A boto3 session.

    Returns:
        Tuple of token endpoint URL, Cognito client id, and client secret.

    Raises:
        RuntimeError: The mcp-tools Lambda (which holds the secret ARN) is absent.
    """
    auth = get_stack_outputs(cfn_client=session.client("cloudformation"), stack_suffix="auth")
    token_endpoint = auth["McpHostedUiDomainUrl"] + "/oauth2/token"
    client_id = auth["M2mClientId"]

    lam = session.client("lambda")
    cfg = None
    for fn_name in (f"{PROJECT_NAME}-mcp-tools", f"{PROJECT_NAME}-dev-mcp-tools"):
        try:
            cfg = lam.get_function_configuration(FunctionName=fn_name)
            break
        except lam.exceptions.ResourceNotFoundException:
            continue
    if cfg is None:
        raise RuntimeError(f"mcp-tools Lambda not found for project {PROJECT_NAME}")
    secret_arn = cfg["Environment"]["Variables"]["M2M_CLIENT_SECRET_ARN"]
    client_secret = session.client("secretsmanager").get_secret_value(
        SecretId=secret_arn
    )["SecretString"]
    return token_endpoint, client_id, client_secret


def fetch_m2m_token(*, creds: Tuple[str, str, str]) -> str:
    """Mint a Cognito client_credentials access token.

    Args:
        creds: (token_endpoint, client_id, client_secret).

    Returns:
        The OAuth access token.
    """
    token_endpoint, client_id, client_secret = creds
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": OAUTH_SCOPE}
    ).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    request = urllib.request.Request(
        token_endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
            "User-Agent": BROWSER_UA,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - Cognito HTTPS endpoint from CFN outputs
        return json.loads(response.read())["access_token"]


def invoke_runtime(
    *, arn: str, session_id: str, payload: bytes, region: str, creds: Tuple[str, str, str]
) -> bytes:
    """Invoke an AgentCore runtime with Cognito Bearer auth.

    Args:
        arn: The runtime ARN.
        session_id: Value for the runtime-session-id header (>= 33 chars).
        payload: JSON-encoded request body.
        region: AWS region.
        creds: M2M credentials tuple for minting the bearer token.

    Returns:
        The raw response body.
    """
    token = fetch_m2m_token(creds=creds)
    encoded_arn = arn.replace(":", "%3A").replace("/", "%2F")
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/"
        f"runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    )
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
            "User-Agent": BROWSER_UA,
        },
    )
    with urllib.request.urlopen(request, timeout=900) as response:  # nosec B310 - AgentCore HTTPS endpoint
        return response.read()


def build_data_sources(*, catalog: str, namespace: str, tables: List[str]) -> List[Dict[str, str]]:
    """Build the S3 Tables data-source dicts the ontology agent expects.

    Args:
        catalog: The federated S3 Tables Athena catalog id (from the manifest).
        namespace: The BIRD db namespace, which is also the Athena database.
        tables: Table names within the namespace.

    Returns:
        One data-source dict per table, in nb11's exact shape.
    """
    return [
        {
            "databaseName": namespace,
            "tableName": table,
            "catalogId": catalog,
            "dataSource": "AwsDataCatalog",
            "tableId": f"{catalog}::{namespace}.{table}",
        }
        for table in tables
    ]


def seed_config(
    *,
    ddb_table: Any,
    layer_id: str,
    namespace: str,
    catalog: str,
    tables: List[str],
) -> str:
    """Write the DynamoDB v1 config record the ontology agent reads on invocation.

    The record's ``name`` carries a UTC timestamp, matching nb11. That timestamp
    ends up in the Neptune named-graph URI, which is why the agent now clears the
    layer's prior graphs before a full rebuild.

    Args:
        ddb_table: The semantic-layer-metadata DynamoDB Table resource.
        layer_id: The deterministic layer id (``vkg-<namespace>``).
        namespace: The BIRD db namespace.
        catalog: The federated S3 Tables catalog id.
        tables: Table names to model.

    Returns:
        The timestamped config name that was written.
    """
    now = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"bird-{namespace}-ontology-{stamp}"
    ddb_table.put_item(
        Item={
            "id": layer_id,
            "version": "v1",
            "type": "VKG",
            "name": name,
            "status": "pending",
            "configuration": {},
            "dataSources": build_data_sources(
                catalog=catalog, namespace=namespace, tables=tables
            ),
            "dataSourcesDescription": (
                f"All {len(tables)} Iceberg S3 Tables in the {namespace} "
                f"BIRD database namespace."
            ),
            "useCasesDescription": VKG_USE_CASES,
            "createdAt": now,
            "updatedAt": now,
            "buildStartedAt": now,
            "createdBy": "rebuild@semantic-layer.local",
        }
    )
    return name


def call_neptune_tool(
    *, lambda_client: Any, function_name: str, tool: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """Invoke a neptune-tools Lambda tool directly, bypassing the gateway.

    The gateway passes the tool name in the Lambda ``client_context`` and the
    arguments as the bare event, so a direct invoke has to reproduce both. Calling
    the Lambda rather than the gateway keeps verification usable from a laptop
    without an M2M token.

    Args:
        lambda_client: A boto3 Lambda client.
        function_name: The neptune-tools function name.
        tool: The bare tool name (e.g. "get_ontology_from_neptune").
        arguments: The tool's argument map.

    Returns:
        The tool's parsed JSON body.
    """
    client_context = base64.b64encode(
        json.dumps({"custom": {"bedrockAgentCoreToolName": f"t___{tool}"}}).encode()
    ).decode()
    response = lambda_client.invoke(
        FunctionName=function_name,
        Payload=json.dumps(arguments).encode(),
        ClientContext=client_context,
    )
    payload = json.loads(response["Payload"].read())
    # The Gateway wraps a Lambda response as {statusCode, body}; unwrap if present.
    if isinstance(payload, dict) and "body" in payload:
        return json.loads(payload["body"])
    return payload


def count_declared_keys(*, lambda_client: Any, function_name: str, layer_id: str) -> Dict[str, Any]:
    """Report how many of a layer's classes carry a declared primary key.

    Read from NEPTUNE, not from the S3 ontology file: the query path reads Neptune,
    so a key present only in S3 changes nothing at query time. This is the check
    that would have caught the two earlier inert fixes.

    Args:
        lambda_client: A boto3 Lambda client.
        function_name: The neptune-tools function name.
        layer_id: The layer id to inspect.

    Returns:
        {"classes": int, "mapped": int, "with_key": int} or {"error": str}.
    """
    try:
        ontology = call_neptune_tool(
            lambda_client=lambda_client,
            function_name=function_name,
            tool="get_ontology_from_neptune",
            arguments={"ontology_id": layer_id},
        )
    except Exception as exc:  # noqa: BLE001 — verification must not mask the build result
        return {"error": f"could not read ontology from Neptune: {exc}"}
    if ontology.get("error"):
        return {"error": ontology["error"]}

    classes = ontology.get("classes") or {}
    mappings = ontology.get("mappings") or {}
    mapped = [
        iri for iri in classes
        if isinstance(mappings.get(iri), dict) and mappings[iri].get("table")
    ]
    with_key = [
        iri for iri in mapped
        if mappings[iri].get("primaryKey") or classes[iri].get("primaryKey")
    ]
    return {"classes": len(classes), "mapped": len(mapped), "with_key": len(with_key)}


def rebuild_one(
    *,
    namespace: str,
    catalog: str,
    tables: List[str],
    runtime_arn: str,
    region: str,
    creds: Tuple[str, str, str],
    metadata_table: str,
    neptune_fn: str,
) -> Dict[str, Any]:
    """Rebuild a single BIRD layer and verify the result in Neptune.

    Args:
        namespace: The BIRD db namespace (also the layer's Athena database).
        catalog: The federated S3 Tables catalog id.
        tables: Table names to model.
        runtime_arn: The ontology runtime ARN to invoke.
        region: AWS region.
        creds: M2M credentials tuple.
        metadata_table: The semantic-layer-metadata DynamoDB table name.
        neptune_fn: The neptune-tools Lambda function name, for verification.

    Returns:
        A result record: layer_id, status, seconds, keys, and summary/error.
    """
    layer_id = f"vkg-{namespace}"
    # A per-thread session: boto3 Sessions are not documented thread-safe, and a
    # shared one across eleven concurrent builds is a real source of flakiness.
    session = boto3.Session()
    ddb_table = session.resource("dynamodb").Table(metadata_table)
    lambda_client = session.client("lambda")

    name = seed_config(
        ddb_table=ddb_table,
        layer_id=layer_id,
        namespace=namespace,
        catalog=catalog,
        tables=tables,
    )
    log(f"[{namespace}] seeded {layer_id} name={name} ({len(tables)} tables)")

    # AgentCore requires a session id of at least 33 characters.
    session_id = f"rebuild-{namespace}-{uuid.uuid4().hex}"
    try:
        invoke_runtime(
            arn=runtime_arn,
            session_id=session_id,
            payload=json.dumps({"id": layer_id}).encode("utf-8"),
            region=region,
            creds=creds,
        )
    except Exception as exc:  # noqa: BLE001 — report per-DB, never abort the fleet
        log(f"[{namespace}] ✗ invoke failed: {exc}")
        return {"layer_id": layer_id, "status": "invoke_failed", "error": str(exc)}

    max_wait = POLL_FLOOR_SECS + POLL_PER_TABLE_SECS * len(tables)
    log(f"[{namespace}] invoked, polling up to {max_wait}s")
    started = time.monotonic()
    status, item = "processing", {}
    last_reported = ""
    while time.monotonic() - started < max_wait:
        time.sleep(POLL_INTERVAL_SECS)
        item = ddb_table.get_item(Key={"id": layer_id, "version": "v1"}).get("Item", {})
        status = item.get("status", "unknown")
        marker = f"{status}/{item.get('phase', '')}/{item.get('progressPercent', '')}"
        if marker != last_reported:
            # Only on change: eleven builds polling every 30s is otherwise unreadable.
            log(f"[{namespace}] [{time.monotonic() - started:.0f}s] {marker}")
            last_reported = marker
        if status in ("completed", "failed"):
            break

    elapsed = round(time.monotonic() - started, 1)
    result: Dict[str, Any] = {
        "layer_id": layer_id,
        "namespace": namespace,
        "status": status,
        "seconds": elapsed,
        "tables": len(tables),
        "summary": item.get("summary") or item.get("error") or f"status={status}",
    }

    if status == "completed":
        result["keys"] = count_declared_keys(
            lambda_client=lambda_client, function_name=neptune_fn, layer_id=layer_id
        )
        keys = result["keys"]
        if keys.get("error"):
            log(f"[{namespace}] ✓ completed in {elapsed}s but verification failed: {keys['error']}")
        else:
            log(
                f"[{namespace}] ✓ completed in {elapsed}s — {keys['with_key']}/"
                f"{keys['mapped']} mapped class(es) carry a declared primary key"
            )
    else:
        log(f"[{namespace}] ✗ {status} after {elapsed}s — {result['summary']}")

    return result


def load_entries(*, dbs: Optional[List[str]]) -> List[Tuple[str, str, List[str]]]:
    """Load (namespace, catalog, tables) for the requested BIRD databases.

    Args:
        dbs: Explicit namespace list, or None for every manifest entry.

    Returns:
        One tuple per database, in manifest order.

    Raises:
        FileNotFoundError: No BIRD manifest exists.
        ValueError: A requested database is not in the manifest.
    """
    bench_dir = Path(__file__).resolve().parents[1] / "data/eval/benchmarks/bird"
    manifest_path = None
    for candidate in (bench_dir / "manifest.json", bench_dir / "deploy_manifest.json"):
        if candidate.exists():
            manifest_path = candidate
            break
    if manifest_path is None:
        raise FileNotFoundError(
            f"No BIRD manifest under {bench_dir}. Run "
            f"scripts/benchmarks/deploy_datasets.py --benchmark bird first."
        )
    manifest = Manifest.load(path=manifest_path)
    entries = [(e.db_id, e.catalog, list(e.tables)) for e in manifest.entries]
    if dbs is None:
        return entries
    by_id = {ns: (ns, cat, tbls) for ns, cat, tbls in entries}
    missing = [d for d in dbs if d not in by_id]
    if missing:
        raise ValueError(
            f"Not in the BIRD manifest: {missing}. Available: {sorted(by_id)}"
        )
    return [by_id[d] for d in dbs]


def main() -> int:
    """Parse arguments, rebuild the requested layers concurrently, and report.

    Returns:
        Process exit code: 0 when every requested layer completed, else 1.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dbs",
        default=None,
        help="Comma-separated namespaces to rebuild (default: every manifest entry)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Concurrent builds. Each is an LLM-heavy agent session; raising this "
             "trades wall-clock for Bedrock throttling risk (default 4)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Do not build; only report declared-primary-key counts from Neptune",
    )
    args = parser.parse_args()

    dbs = [d.strip() for d in args.dbs.split(",") if d.strip()] if args.dbs else None
    entries = load_entries(dbs=dbs)

    session = boto3.Session()
    region = session.region_name or os.environ.get("AWS_REGION", "us-east-1")
    # Default matches the deployed name (the stack interpolates the 'dev' suffix);
    # notebooks/.env sets the same value, so an env override wins when present.
    metadata_table = os.environ.get(
        "ONTOLOGY_METADATA_TABLE", f"{PROJECT_NAME}-dev-metadata"
    )
    neptune_fn = os.environ.get("NEPTUNE_TOOLS_FUNCTION", f"{PROJECT_NAME}-dev-neptune-tools-v2")

    if args.verify_only:
        lambda_client = session.client("lambda")
        print(f"Verifying {len(entries)} layer(s) against Neptune via {neptune_fn}\n")
        failures = 0
        for namespace, _catalog, _tables in entries:
            keys = count_declared_keys(
                lambda_client=lambda_client,
                function_name=neptune_fn,
                layer_id=f"vkg-{namespace}",
            )
            if keys.get("error"):
                failures += 1
                print(f"  ✗ {namespace}: {keys['error']}")
            else:
                mark = "✓" if keys["with_key"] else "⚠"
                if not keys["with_key"]:
                    failures += 1
                print(
                    f"  {mark} {namespace}: {keys['with_key']}/{keys['mapped']} mapped "
                    f"class(es) with a declared primary key ({keys['classes']} classes)"
                )
        return 1 if failures else 0

    outputs = get_stack_outputs(cfn_client=session.client("cloudformation"), stack_suffix="agentcore")
    runtime_arn = outputs["OntologyRuntimeArn"]
    creds = get_m2m_creds(session=session)

    print(
        f"Rebuilding {len(entries)} BIRD layer(s) with concurrency {args.concurrency}\n"
        f"  runtime: {runtime_arn.rsplit('/', 1)[-1]}\n"
        f"  region : {region}\n"
    )

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                rebuild_one,
                namespace=namespace,
                catalog=catalog,
                tables=tables,
                runtime_arn=runtime_arn,
                region=region,
                creds=creds,
                metadata_table=metadata_table,
                neptune_fn=neptune_fn,
            ): namespace
            for namespace, catalog, tables in entries
        }
        for future in as_completed(futures):
            namespace = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 — one DB must not sink the run
                log(f"[{namespace}] ✗ driver error: {exc}")
                results.append(
                    {"layer_id": f"vkg-{namespace}", "namespace": namespace,
                     "status": "driver_error", "error": str(exc)}
                )

    results.sort(key=lambda r: r.get("namespace", ""))
    completed = [r for r in results if r.get("status") == "completed"]
    with_keys = [
        r for r in completed
        if isinstance(r.get("keys"), dict) and r["keys"].get("with_key")
    ]

    print("\n" + "=" * 72)
    print(f"REBUILD SUMMARY — {len(completed)}/{len(results)} completed, "
          f"{len(with_keys)}/{len(completed)} carry declared primary keys")
    print("=" * 72)
    for r in results:
        keys = r.get("keys") or {}
        key_note = (
            f"keys={keys['with_key']}/{keys['mapped']}"
            if keys.get("mapped") is not None else keys.get("error", "")
        )
        print(f"  {r.get('status'):<14} {r.get('namespace', ''):<32} "
              f"{r.get('seconds', '')!s:>8}s  {key_note}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = (
        Path(__file__).resolve().parents[1]
        / f"data/eval/results/bird_layer_rebuild_{stamp}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {out_path}")

    return 0 if len(completed) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
