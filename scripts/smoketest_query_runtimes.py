"""Post-deploy smoketest for the VKG / semantic-RAG query runtimes.

Invokes each query runtime once over the CUSTOM_JWT (Cognito M2M) path — the
same path the eval notebooks use — and reports whether a grounded answer came
back inside the client timeout.

Purpose: after a redeploy, confirm the container actually boots and answers
before committing to a multi-hour benchmark eval. A container that fails to
import (e.g. a missing module in the flat container layout) still reports the
runtime as READY, so READY alone is not evidence the deploy is good.

Run:
    python scripts/smoketest_query_runtimes.py
Exit code 0 iff every targeted runtime returned a non-empty answer.
"""

import base64
import json
import sys
import time
import urllib.parse as _urlparse
import urllib.request as _urlrequest
from typing import Any, Dict, Optional, Tuple

import boto3

# The chat-stream entrypoint (message/turnId/sessionId) is the path the eval
# notebooks drive; the bare {"question": ...} entrypoint is a different code
# path, so reuse the notebooks' own payload/parse helpers here.
try:
    from agents.shared.eval_multiturn import build_chat_payload, parse_chat_stream_sse
except ImportError:  # running from the repo root without the package installed
    sys.path.insert(0, ".")
    from agents.shared.eval_multiturn import build_chat_payload, parse_chat_stream_sse

PROJECT_NAME = "semantic-layer"
REGION = "us-east-1"
_OAUTH_SCOPE = "semantic-layer-mcp/invoke"
# AgentCore's front door rejects the default urllib UA, so present a browser UA.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Matches nb12's client ceiling so the smoketest fails for the same reason the
# eval would, rather than masking a slow path with a longer timeout.
_INVOKE_TIMEOUT_SECONDS = 900

session = boto3.Session(region_name=REGION)


def _get_m2m_creds() -> Tuple[str, str, str]:
    """Resolve the Cognito M2M token endpoint and client credentials.

    Returns:
        Tuple of (token_endpoint str, client_id str, client_secret str).
    """
    cfn_client = session.client("cloudformation", region_name=REGION)
    auth_outputs: Dict[str, str] = {}
    for stack_name in (f"{PROJECT_NAME}-auth", f"{PROJECT_NAME}-dev-auth"):
        try:
            stacks = cfn_client.describe_stacks(StackName=stack_name)["Stacks"]
            auth_outputs = {
                o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])
            }
            break
        except Exception:  # noqa: BLE001 — try the next naming variant
            continue
    if not auth_outputs:
        raise RuntimeError(f"Auth stack not found for project {PROJECT_NAME}")

    token_endpoint = auth_outputs["McpHostedUiDomainUrl"] + "/oauth2/token"
    client_id = auth_outputs["M2mClientId"]

    lam = session.client("lambda")
    cfg: Optional[Dict[str, Any]] = None
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
        SecretId=secret_arn,
    )["SecretString"]
    return token_endpoint, client_id, client_secret


def _fetch_m2m_token() -> str:
    """Mint a Cognito client_credentials access token.

    Returns:
        The OAuth access_token string.
    """
    token_endpoint, client_id, client_secret = _get_m2m_creds()
    body = _urlparse.urlencode(
        {"grant_type": "client_credentials", "scope": _OAUTH_SCOPE},
    ).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    req = _urlrequest.Request(
        token_endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
            "User-Agent": _BROWSER_UA,
        },
    )
    with _urlrequest.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


def _invoke_runtime(*, arn: str, session_id: str, payload: bytes,
                    token: str, qualifier: str = "DEFAULT") -> bytes:
    """Invoke an AgentCore runtime with Cognito Bearer auth.

    Parameters:
        arn: The runtime ARN.
        session_id: Value for the AgentCore runtime session-id header.
        payload: JSON-encoded request body.
        token: A Cognito M2M bearer token.
        qualifier: Runtime qualifier to target.
    Returns:
        Raw response body bytes.
    """
    encoded_arn = arn.replace(":", "%3A").replace("/", "%2F")
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com/"
        f"runtimes/{encoded_arn}/invocations?qualifier={qualifier}"
    )
    req = _urlrequest.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
            "User-Agent": _BROWSER_UA,
        },
    )
    with _urlrequest.urlopen(req, timeout=_INVOKE_TIMEOUT_SECONDS) as resp:
        return resp.read()


def _runtime_arn(*, runtime_name: str) -> str:
    """Look up a runtime ARN by exact runtime name.

    Parameters:
        runtime_name: The AgentCore runtime name (without version suffix).
    Returns:
        The runtime ARN.
    """
    control = session.client("bedrock-agentcore-control")
    paginator = control.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for rt in page["agentRuntimes"]:
            if rt["agentRuntimeName"] == runtime_name:
                return rt["agentRuntimeArn"]
    raise RuntimeError(f"Runtime not found: {runtime_name}")


def _extract_result(*, raw: bytes) -> Dict[str, Any]:
    """Parse the runtime's AG-UI SSE chat-stream body into a flat result dict.

    Delegates to the same helper the eval notebooks use, so the smoketest reads
    the response exactly as nb12/nb14 will.

    Parameters:
        raw: Raw response bytes from the runtime.
    Returns:
        Dict with at least ``answer``, ``sql`` and ``error`` keys.
    """
    return parse_chat_stream_sse(raw.decode("utf-8", errors="replace"))


# (label, runtime name, ontologyId) — ontologyId binds the agent to a layer, and
# the id must exist in the semantic-layer-dev-metadata registry: an unknown id
# yields an immediate run_finished with an empty answer, which looks like a boot
# failure but is really an unresolvable layer binding.
TARGETS = [
    ("VKG", "semantic_layer_dev_ontology_query", "vkg-bird_financial"),
    ("Semantic RAG", "semantic_layer_dev_metadata_query",
     "semantic-rag-multi_table_curated_layer-db96de4d"),
]

QUESTION = "How many rows are in the largest table? Answer with a single number."


def main() -> int:
    """Smoketest each query runtime once.

    Returns:
        0 if every runtime answered, 1 otherwise.
    """
    token = _fetch_m2m_token()
    print("✓ minted M2M token")

    failures = 0
    for label, runtime_name, ontology_id in TARGETS:
        arn = _runtime_arn(runtime_name=runtime_name)
        session_id = f"smoketest-{runtime_name}-{int(time.monotonic() * 1000)}"
        payload = json.dumps(build_chat_payload(
            message=QUESTION,
            session_id=session_id,
            ontology_id=ontology_id,
            turn_idx=0,
            source="eval",
        )).encode()
        print(f"\n▶ {label} ({runtime_name}) ontologyId={ontology_id}")
        started = time.monotonic()
        try:
            raw = _invoke_runtime(
                arn=arn, session_id=session_id, payload=payload, token=token,
            )
        except Exception as exc:  # noqa: BLE001 — report and continue to next target
            print(f"  ❌ invoke failed after {time.monotonic() - started:.1f}s: {exc}")
            failures += 1
            continue

        elapsed = time.monotonic() - started
        result = _extract_result(raw=raw)
        answer = (result.get("answer") or "").strip()
        if answer:
            sql = (result.get("sql") or "").strip()
            print(f"  ✅ answered in {elapsed:.1f}s: {answer[:300]}")
            print(f"     sql: {(sql[:200] + '...') if len(sql) > 200 else (sql or '(none)')}")
        else:
            err = result.get("error")
            print(f"  ❌ empty answer after {elapsed:.1f}s; error={err!r}; body={raw[:300]!r}")
            failures += 1

    print(f"\n{'🏁 ALL PASS' if failures == 0 else f'❌ {failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
