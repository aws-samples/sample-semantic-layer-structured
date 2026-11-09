#!/usr/bin/env python3
"""Build a Hybrid semantic layer over the same tables as an existing VKG layer.

The Hybrid layer type has no admin-UI wizard yet, so this driver replicates the
flow the wizard would perform:

  1. copy the ``dataSources`` (and the domain-context descriptions) from a
     reference layer so the Hybrid arm is evaluated over the SAME tables as the
     SemanticRAG and VKG arms — otherwise notebook 16's three-way comparison is
     not apples-to-apples;
  2. seed the ``hybrid-*`` DynamoDB config record;
  3. invoke the Hybrid creation runtime with an M2M OAuth bearer token;
  4. poll DynamoDB until the build reaches a terminal status;
  5. verify the thin VKG actually landed in Neptune — non-empty graph WITH
     ``mapsToColumn`` triples. A classes-only graph cannot ground any SQL, so an
     "empty but successful" build is treated as a failure.

Usage:
    python scripts/build_hybrid_layer.py      # copies the best-coverage VKG layer
    python scripts/build_hybrid_layer.py --reference-layer vkg-ontology_curated_layer-ed486e6f
    python scripts/build_hybrid_layer.py --verify-only --layer-id hybrid-curated_layer
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import boto3

# Cognito M2M scope the AgentCore runtimes' CUSTOM_JWT authorizer accepts.
OAUTH_SCOPE = "semantic-layer-mcp/invoke"

# The Cognito hosted-UI domain fronts /oauth2/token behind a WAF that returns a
# bare HTTP 403 for non-browser User-Agents (urllib's default is rejected). The
# eval notebooks send this same string for the same reason.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# The named graph the creation agent writes each layer into. MUST stay in lockstep
# with semantic_layer_creation_agent.main::vkg_graph_iri.
VKG_GRAPH_PREFIX = "https://semantic-layer.aws/vkg/"

# The mapping-predicate vocabulary both hybrid agents share.
VIRTUAL_KG_VOCAB = "https://semantic-layer.aws/virtual-kg/"

# A build has to finish inside this budget or we fail loudly. Per table the KB
# phase runs a Glue read, one Athena value-probe PER COLUMN, and a Bedrock prose
# call; the VKG phase adds another Glue read. The per-column probes dominate, so
# scale with table count rather than using a flat ceiling.
POLL_FLOOR_SECS = 900
POLL_SECS_PER_TABLE = 300


def _stack_outputs(*, cfn: Any, project: str, suffix: str) -> Dict[str, str]:
    """Return a CloudFormation stack's outputs as a flat dict.

    Tries both the bare (``<project>-<suffix>``) and ``-dev-`` infixed stack names
    because the deployed stack name depends on the CDK stage.

    Parameters:
        cfn: A boto3 CloudFormation client.
        project: The project name prefix (e.g. ``semantic-layer-dev``).
        suffix: The stack suffix (e.g. ``agentcore`` or ``auth``).
    Returns:
        dict: OutputKey -> OutputValue.
    Raises:
        RuntimeError: Neither stack-name variant exists.
    """
    for name in (f"{project}-{suffix}", f"{project}-dev-{suffix}"):
        try:
            stacks = cfn.describe_stacks(StackName=name)["Stacks"]
        except Exception:  # noqa: BLE001 — any lookup failure means "try the next name"
            continue
        return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    raise RuntimeError(f"Stack {project}-[dev-]{suffix} not found")


def _m2m_token(*, session: Any, project: str) -> str:
    """Mint a Cognito client_credentials access token for runtime invocation.

    Parameters:
        session: A boto3 Session.
        project: The project name prefix.
    Returns:
        str: The OAuth access token.
    Raises:
        RuntimeError: The auth stack, the mcp-tools Lambda, or the token response
            is missing what we need.
    """
    cfn = session.client("cloudformation")
    auth = _stack_outputs(cfn=cfn, project=project, suffix="auth")
    token_endpoint = auth["McpHostedUiDomainUrl"] + "/oauth2/token"
    client_id = auth["M2mClientId"]

    # The client secret lives in Secrets Manager; the mcp-tools Lambda's env holds
    # the ARN, which is the same indirection the notebooks use.
    lam = session.client("lambda")
    cfg = None
    for fn in (f"{project}-mcp-tools", f"{project}-dev-mcp-tools"):
        try:
            cfg = lam.get_function_configuration(FunctionName=fn)
            break
        except lam.exceptions.ResourceNotFoundException:
            continue
    if cfg is None:
        raise RuntimeError(f"mcp-tools Lambda not found for project {project}")
    secret_arn = cfg["Environment"]["Variables"]["M2M_CLIENT_SECRET_ARN"]
    client_secret = session.client("secretsmanager").get_secret_value(
        SecretId=secret_arn
    )["SecretString"]

    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": OAUTH_SCOPE}
    ).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    req = urllib.request.Request(
        token_endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
            "User-Agent": BROWSER_UA,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https endpoint
        payload = json.loads(resp.read().decode())
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in Cognito response: {payload}")
    return token


def _read_reference_layer(*, table: Any, layer_id: str) -> Dict[str, Any]:
    """Read a completed reference layer whose dataSources the Hybrid arm copies.

    Parameters:
        table: A boto3 DynamoDB Table resource for the metadata table.
        layer_id: The reference layer's partition key.
    Returns:
        dict: The reference layer item.
    Raises:
        RuntimeError: No completed item exists for that id, or it has no
            dataSources — copying an empty table list would silently build a
            layer that can answer nothing.
    """
    resp = table.query(
        KeyConditionExpression="id = :i", ExpressionAttributeValues={":i": layer_id}
    )
    for item in resp.get("Items", []):
        if item.get("status") == "completed":
            if not item.get("dataSources"):
                raise RuntimeError(
                    f"Reference layer {layer_id} has no dataSources — refusing to "
                    f"build a Hybrid layer over zero tables."
                )
            return item
    raise RuntimeError(f"No completed reference layer found for id={layer_id!r}")


def _best_coverage_vkg_layer(*, table: Any) -> str:
    """Return the completed ``vkg-ontology*`` layer with the most tables.

    Same rule notebook 5 uses to pick the layer it evaluates (newest breaks ties),
    so the Hybrid arm is built over exactly the tables the VKG arm was scored on.

    Parameters:
        table: A boto3 DynamoDB Table resource for the metadata table.
    Returns:
        str: The winning layer id.
    Raises:
        RuntimeError: No completed VKG ontology layer exists yet.
    """
    items: List[Dict[str, Any]] = []
    scan_kw: Dict[str, Any] = {
        "FilterExpression": "contains(id, :p) AND #s = :c",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":p": "vkg-ontology", ":c": "completed"},
    }
    while True:
        page = table.scan(**scan_kw)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        scan_kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if not items:
        raise RuntimeError(
            "No completed vkg-ontology* layer to copy dataSources from. Run "
            "notebook 4 first, or pass --reference-layer explicitly."
        )
    best = max(
        items,
        key=lambda it: (
            len(it.get("dataSources") or []),
            it.get("updatedAt") or it.get("createdAt") or "",
        ),
    )
    return best["id"]


def _seed_hybrid_config(
    *,
    table: Any,
    layer_id: str,
    reference: Dict[str, Any],
) -> int:
    """Write the ``hybrid-*`` config record the creation agent reads.

    Matches the shape the admin wizard produces for the other layer types (see
    notebook 13's ``seed_test_ontology``): ``version='v1'``, ``status='pending'``,
    the copied ``dataSources``, and the two domain-context description fields the
    KB-prose phase feeds to the model.

    Parameters:
        table: A boto3 DynamoDB Table resource for the metadata table.
        layer_id: The ``hybrid-*`` id to create.
        reference: The reference layer item whose sources/context are copied.
    Returns:
        int: The number of dataSources written.
    """
    now = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    data_sources: List[Dict[str, str]] = [
        {
            "databaseName": ds["databaseName"],
            "tableName": ds["tableName"],
            "catalogId": ds.get("catalogId", "AWSDataCatalog"),
            "dataSource": ds.get("dataSource", "AwsDataCatalog"),
            "tableId": ds.get(
                "tableId",
                f"{ds.get('catalogId', 'AWSDataCatalog')}::"
                f"{ds['databaseName']}.{ds['tableName']}",
            ),
        }
        for ds in reference["dataSources"]
        if ds.get("databaseName") and ds.get("tableName")
    ]
    item: Dict[str, Any] = {
        "id": layer_id,
        "version": "v1",
        "type": "Hybrid",
        "name": f"hybrid-eval-{stamp}",
        "status": "pending",
        "configuration": {},
        "dataSources": data_sources,
        "createdAt": now,
        "updatedAt": now,
        "buildStartedAt": now,
        "createdBy": "eval@semantic-layer.local",
    }
    # Carry the domain context across: the KB-prose phase reads both fields, and
    # dropping them would make the Hybrid arm's prose weaker than the VKG arm's
    # for reasons unrelated to the architecture under test.
    for field in ("dataSourcesDescription", "useCasesDescription"):
        if reference.get(field):
            item[field] = reference[field]
    table.put_item(Item=item)
    return len(data_sources)


def _invoke_creation_runtime(
    *, session: Any, project: str, runtime_arn: str, layer_id: str, token: str
) -> None:
    """Invoke the Hybrid creation runtime for ``layer_id``.

    The runtime processes the build synchronously inside the invocation, which can
    exceed the HTTP read timeout; that is fine because progress is written to
    DynamoDB and :func:`_poll_until_terminal` is the real completion signal. A
    timeout here is therefore logged, not raised.

    Parameters:
        session: A boto3 Session.
        project: The project name prefix (unused beyond symmetry with callers).
        runtime_arn: The Hybrid creation runtime ARN.
        layer_id: The layer id to build (also the AgentCore session id).
        token: An M2M OAuth bearer token.
    Returns:
        None.
    """
    region = session.region_name
    escaped = urllib.parse.quote(runtime_arn, safe="")
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{escaped}/invocations?qualifier=DEFAULT"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps({"id": layer_id}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            # Session ids must be >= 33 chars; pad the layer id deterministically.
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"{layer_id}-{'0' * 40}"[:64],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            print(f"  runtime responded {resp.status}: {resp.read()[:400]!r}")
    except Exception as exc:  # noqa: BLE001 — see docstring: DynamoDB is the signal
        print(f"  (invoke returned {type(exc).__name__}: {exc}; polling DynamoDB)")


def _poll_until_terminal(
    *, table: Any, layer_id: str, budget_secs: int
) -> Dict[str, Any]:
    """Poll the layer record until it reaches ``completed`` or ``failed``.

    Parameters:
        table: A boto3 DynamoDB Table resource for the metadata table.
        layer_id: The layer being built.
        budget_secs: How long to wait before giving up.
    Returns:
        dict: The terminal item.
    Raises:
        RuntimeError: The build failed, or the budget expired (the record is left
            in place for inspection — we do NOT report a stalled build as done).
    """
    deadline = time.time() + budget_secs
    last_note = ""
    while time.time() < deadline:
        resp = table.get_item(Key={"id": layer_id, "version": "v1"}, ConsistentRead=True)
        item = resp.get("Item") or {}
        status = item.get("status", "?")
        note = f"{status} {item.get('progressMessage', '')}".strip()
        if note != last_note:
            # flush=True: a redirected run is block-buffered otherwise, which
            # makes a long build look hung when it is progressing fine.
            print(f"  [{int(time.time() - (deadline - budget_secs))}s] {note}", flush=True)
            last_note = note
        if status == "completed":
            return item
        if status == "failed":
            raise RuntimeError(f"Hybrid build FAILED: {item.get('error', '?')}")
        time.sleep(20)
    raise RuntimeError(
        f"Hybrid build for {layer_id} did not reach a terminal status within "
        f"{budget_secs}s (last: {last_note!r})"
    )


def _sparql_count(*, lambda_client: Any, fn_name: str, query: str) -> int:
    """Run a SPARQL SELECT COUNT through the neptune-tools Lambda; return the count.

    Invoking the Lambda directly (rather than through the gateway) keeps this
    verification independent of the gateway's MCP layer, so a gateway problem is
    distinguishable from an empty graph.

    Parameters:
        lambda_client: A boto3 Lambda client.
        fn_name: The neptune-tools function name.
        query: A SPARQL SELECT returning a single ``?n`` binding.
    Returns:
        int: The count value.
    Raises:
        RuntimeError: The tool errored or returned an unparseable shape.
    """
    # The neptune-tools Lambda follows the AgentCore Gateway calling convention: the
    # tool NAME arrives in the Lambda ClientContext (custom.bedrockAgentCoreToolName)
    # and the event body is the flat argument map — NOT a {tool_name, arguments}
    # envelope. Passing the envelope leaves tool_name None and the handler answers
    # "Unknown tool: None". ClientContext is a base64-encoded JSON blob.
    client_context = base64.b64encode(
        json.dumps(
            {"custom": {"bedrockAgentCoreToolName": "execute_sparql_query"}}
        ).encode()
    ).decode("ascii")
    resp = lambda_client.invoke(
        FunctionName=fn_name,
        ClientContext=client_context,
        Payload=json.dumps(
            {"sparql_query": query, "query_type": "SELECT"}
        ).encode(),
    )
    raw = resp["Payload"].read().decode()
    outer = json.loads(raw)
    body = outer.get("body", raw) if isinstance(outer, dict) else raw
    payload = json.loads(body) if isinstance(body, str) else body
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"SPARQL count failed: {payload['error']}")
    try:
        bindings = payload["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected SPARQL response shape: {payload}") from exc
    if not bindings:
        return 0
    return int(bindings[0]["n"]["value"])


def _verify_graph(*, session: Any, project: str, layer_id: str) -> Tuple[int, int]:
    """Verify the layer's Neptune graph is non-empty and carries column mappings.

    A classes-only graph (the pre-2026-08-05 ``columns=[]`` bug) looks like a
    successful build but cannot ground a single column reference, so the
    ``mapsToColumn`` count is checked separately from the total.

    Parameters:
        session: A boto3 Session.
        project: The project name prefix.
        layer_id: The built layer id.
    Returns:
        tuple: (total triple count, mapsToColumn triple count).
    Raises:
        RuntimeError: The neptune-tools Lambda is missing, the graph is empty, or
            it has no column mappings.
    """
    lam = session.client("lambda")
    fn_name = None
    # The deployed function carries a version suffix (`-v2`) because the gateway
    # target was recreated; try the suffixed names FIRST so a stale unsuffixed
    # function (if one is ever left behind) does not shadow the live one.
    for candidate in (
        f"{project}-neptune-tools-v2",
        f"{project}-dev-neptune-tools-v2",
        f"{project}-neptune-tools",
        f"{project}-dev-neptune-tools",
    ):
        try:
            lam.get_function_configuration(FunctionName=candidate)
            fn_name = candidate
            break
        except lam.exceptions.ResourceNotFoundException:
            continue
    if fn_name is None:
        raise RuntimeError(f"neptune-tools Lambda not found for project {project}")

    graph = f"{VKG_GRAPH_PREFIX}{layer_id}"
    total = _sparql_count(
        lambda_client=lam,
        fn_name=fn_name,
        query=(
            f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}"
        ),
    )
    columns = _sparql_count(
        lambda_client=lam,
        fn_name=fn_name,
        query=(
            f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph}> "
            f"{{ ?s <{VIRTUAL_KG_VOCAB}mapsToColumn> ?o }} }}"
        ),
    )
    if total == 0:
        raise RuntimeError(
            f"Neptune graph <{graph}> is EMPTY — the thin VKG never landed, so "
            f"every hybrid query would abstain as ungrounded."
        )
    if columns == 0:
        raise RuntimeError(
            f"Neptune graph <{graph}> has {total} triples but ZERO mapsToColumn "
            f"mappings — a classes-only VKG cannot ground any column reference."
        )
    return total, columns


def main() -> int:
    """Build (or verify) a Hybrid semantic layer. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        default=os.environ.get("PROJECT_NAME", "semantic-layer-dev"),
        help="Project name prefix used for stack/table/function lookups.",
    )
    parser.add_argument(
        "--reference-layer",
        default="",
        help=(
            "Completed layer whose dataSources the Hybrid layer copies. Default: "
            "the completed vkg-ontology* layer with the most tables."
        ),
    )
    parser.add_argument(
        "--layer-id",
        default="hybrid-curated_layer",
        help="The hybrid-* layer id to create.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip seeding/invoking; only verify the Neptune graph.",
    )
    args = parser.parse_args()

    session = boto3.Session()
    table = session.resource("dynamodb").Table(f"{args.project}-metadata")

    if not args.verify_only:
        if not args.reference_layer:
            args.reference_layer = _best_coverage_vkg_layer(table=table)
            print(f"→ Reference layer (best-coverage VKG): {args.reference_layer}")
        reference = _read_reference_layer(
            table=table, layer_id=args.reference_layer
        )
        n_tables = _seed_hybrid_config(
            table=table, layer_id=args.layer_id, reference=reference
        )
        print(
            f"✓ Seeded {args.layer_id} with {n_tables} table(s) copied from "
            f"{args.reference_layer}"
        )

        cfn = session.client("cloudformation")
        ac = _stack_outputs(cfn=cfn, project=args.project, suffix="agentcore")
        runtime_arn = ac["SemanticLayerCreationRuntimeArn"]
        print(f"→ Invoking Hybrid creation runtime {runtime_arn.rsplit('/', 1)[-1]}")
        _invoke_creation_runtime(
            session=session,
            project=args.project,
            runtime_arn=runtime_arn,
            layer_id=args.layer_id,
            token=_m2m_token(session=session, project=args.project),
        )

        budget = POLL_FLOOR_SECS + POLL_SECS_PER_TABLE * n_tables
        print(f"→ Polling DynamoDB (budget {budget}s)")
        _poll_until_terminal(
            table=table, layer_id=args.layer_id, budget_secs=budget
        )
        print("✓ Build reached status=completed")

    total, columns = _verify_graph(
        session=session, project=args.project, layer_id=args.layer_id
    )
    print(
        f"✓ Neptune graph <{VKG_GRAPH_PREFIX}{args.layer_id}>: {total} triples, "
        f"{columns} mapsToColumn mapping(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
