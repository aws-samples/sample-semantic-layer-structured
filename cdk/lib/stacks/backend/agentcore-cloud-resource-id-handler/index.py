"""
Custom resource handler: post-creation env var injection for AgentCore Runtimes.

Injects three values that cannot be set at CFN create time (circular dependency —
a runtime can't reference its own not-yet-known ARN/ID in its own env vars):

1. cloud.resource_id  → OTEL_RESOURCE_ATTRIBUTES
   Required by bedrock_agentcore_starter_toolkit span query:
   `parse resource.attributes.cloud.resource_id "runtime/*/" as parsedAgentId`

2. aws.log.group.names → OTEL_RESOURCE_ATTRIBUTES
3. x-aws-log-group    → OTEL_EXPORTER_OTLP_LOGS_HEADERS

   Items 2 & 3 redirect OTLP log records to the per-deployment log group
   `/aws/bedrock-agentcore/runtimes/{runtimeId}-DEFAULT`.
   The SDK's query_runtime_logs_by_traces hardcodes that pattern:
     f"/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT"
   so OTLP logs must land there for on-demand evaluation to find them.

AUTHORITATIVE FULL-REPLACE (self-healing): ``update_agent_runtime`` is a
FULL-REPLACE API — every field the call omits is RESET on the runtime. This handler
is the SINGLE custom resource that owns that call, so it must re-send the runtime's
COMPLETE intended state on every invocation or some field gets silently wiped. It
sources the authoritative state from CFN props (synthesized by CDK) so each deploy
RESTORES — not merely preserves — that state, healing any prior drift:

  * environmentVariables    ← ``BaseEnvironmentVariables`` prop (CDK source of truth),
                              then the cloud.resource_id / log-group patches layered on.
  * authorizerConfiguration ← ``DiscoveryUrl`` + ``AllowedClients`` props
                              (CUSTOM_JWT). Omitted when not supplied → the runtime
                              keeps its IAM inbound-auth fallback.
  * requestHeaderConfiguration ← ``AllowlistedHeaders`` prop, else the live value is
                              echoed (don't wipe), else omitted.
  * protocol/lifecycle/metadata/description ← echoed from the live runtime when present.

WHY THIS HANDLER OWNS THE AUTHORIZER: ``AWS::BedrockAgentCore::Runtime`` silently
drops ``AuthorizerConfiguration`` on CREATE, so the JWT authorizer must be applied
post-create via the control plane. This used to live in a SEPARATE custom resource
(runtime-authorizer-handler) that fired only when allowedClients/headers changed.
But a code-only container redeploy changes the image tag (this resource's fingerprint)
WITHOUT changing the authorizer inputs — so THIS handler fired a full-replace that
omitted the authorizer and wiped it, while the separate authorizer resource stayed
dormant. Folding the authorizer into this one handler means a single full-replace
re-sends EVERYTHING every time, so no field can be dropped. See
docs/plans/2026-06-04-runtime-authorizer-wipe-design.md.

Bundled with boto3>=1.43.21 (requirements.txt) so the ``authorizerConfiguration`` /
``requestHeaderConfiguration`` update params are available (the Lambda runtime's
built-in boto3 predates them).
"""

import logging
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _replace_kv(csv: str, key: str, value: str) -> str:
    """Replace or append a key=value pair in a comma-separated string."""
    prefix = f"{key}="
    parts = [p for p in csv.split(",") if not p.startswith(prefix)]
    parts.append(f"{prefix}{value}")
    return ",".join(parts)


def _build_authorizer(props: dict[str, Any]) -> dict[str, Any] | None:
    """Build the CUSTOM_JWT authorizerConfiguration from CFN props, or None.

    :param props: the ResourceProperties dict. Reads ``DiscoveryUrl`` (str, the
        OIDC discovery URL) and ``AllowedClients`` (list[str], allowed JWT client
        ids). Both must be present and non-empty to build an authorizer.
    :type props: dict[str, typing.Any]
    :returns: ``{'customJWTAuthorizer': {'discoveryUrl': ..., 'allowedClients': ...}}``
        when both inputs are supplied, else ``None`` (runtime keeps IAM inbound auth).
    :rtype: dict[str, typing.Any] | None
    """
    discovery_url = props.get("DiscoveryUrl")
    allowed_clients = props.get("AllowedClients")
    if not discovery_url or not allowed_clients:
        return None
    return {
        "customJWTAuthorizer": {
            "discoveryUrl": discovery_url,
            "allowedClients": allowed_clients,
        }
    }


def on_event(event, context):  # noqa: ARG001
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    runtime_arn: str = props["AgentRuntimeArn"]
    region: str = props["Region"]

    # Extract just the runtime ID (last segment of ARN path: "runtime/<id>")
    runtime_id = runtime_arn.rsplit("/", 1)[-1]

    logger.info("RequestType=%s runtimeId=%s", request_type, runtime_id)

    physical_id = f"cloud-resource-id-{runtime_id}"

    if request_type == "Delete":
        # Nothing to undo — leave env vars in place.
        return {"PhysicalResourceId": physical_id}

    logs_client = boto3.client("logs", region_name=region)
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    # Get current runtime configuration (required fields for the update call)
    r = client.get_agent_runtime(agentRuntimeId=runtime_id)

    # AUTHORITATIVE ENV: prefer the CFN-supplied base env block (source of truth
    # synthesized by CDK). This RESTORES env on every deploy, healing any prior
    # out-of-band update_agent_runtime that wiped it. Only fall back to the live
    # runtime env when the property is absent (older stack revisions).
    base_env = props.get("BaseEnvironmentVariables")
    if base_env:
        env = dict(base_env)
        logger.info("Using CFN-supplied BaseEnvironmentVariables (%d vars)", len(env))
    else:
        env = dict(r.get("environmentVariables") or {})
        logger.info(
            "BaseEnvironmentVariables absent; falling back to live runtime env (%d vars)",
            len(env),
        )

    # 1. cloud.resource_id — trailing slash required for CWL Insights glob parse
    cloud_resource_id_value = f"{runtime_arn}/"
    env["OTEL_RESOURCE_ATTRIBUTES"] = _replace_kv(
        env.get("OTEL_RESOURCE_ATTRIBUTES", ""),
        "cloud.resource_id",
        cloud_resource_id_value,
    )
    logger.info("Set cloud.resource_id=%s", cloud_resource_id_value)

    # 2 & 3. Redirect OTLP log records to {runtimeId}-DEFAULT so the SDK's
    #         query_runtime_logs_by_traces can find them during on-demand eval.
    eval_log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"

    env["OTEL_RESOURCE_ATTRIBUTES"] = _replace_kv(
        env["OTEL_RESOURCE_ATTRIBUTES"],
        "aws.log.group.names",
        eval_log_group,
    )

    env["OTEL_EXPORTER_OTLP_LOGS_HEADERS"] = _replace_kv(
        env.get("OTEL_EXPORTER_OTLP_LOGS_HEADERS", ""),
        "x-aws-log-group",
        eval_log_group,
    )

    logger.info("Set aws.log.group.names=%s", eval_log_group)
    logger.info("Set x-aws-log-group=%s", eval_log_group)
    logger.info("Final OTEL_RESOURCE_ATTRIBUTES: %s", env["OTEL_RESOURCE_ATTRIBUTES"])
    logger.info("Final OTEL_EXPORTER_OTLP_LOGS_HEADERS: %s", env["OTEL_EXPORTER_OTLP_LOGS_HEADERS"])

    # Ensure the runtime-logs stream exists in the eval log group.
    # The OTEL exporter uses x-aws-log-stream=runtime-logs; without this stream
    # the export fails with 400 "The specified log stream does not exist."
    try:
        logs_client.create_log_stream(
            logGroupName=eval_log_group,
            logStreamName="runtime-logs",
        )
        logger.info("Created log stream 'runtime-logs' in %s", eval_log_group)
    except logs_client.exceptions.ResourceAlreadyExistsException:
        logger.info("Log stream 'runtime-logs' already exists in %s", eval_log_group)

    # FULL-REPLACE payload: REQUIRED fields + env (restored above). Anything not
    # included here is reset on the runtime, so build up the complete intended
    # state before the single update call.
    update_kwargs: dict[str, Any] = {
        "agentRuntimeId": runtime_id,
        "environmentVariables": env,
        "roleArn": r["roleArn"],
        "agentRuntimeArtifact": r["agentRuntimeArtifact"],
        "networkConfiguration": r["networkConfiguration"],
    }

    # authorizerConfiguration — AUTHORITATIVE from CFN props (CUSTOM_JWT). When the
    # props are absent (no user pool / IAM-fallback runtime) we omit it; the runtime
    # keeps its IAM inbound auth. CFN drops AuthorizerConfiguration on CREATE, so
    # re-sending it here is what actually applies/persists the JWT authorizer.
    authorizer = _build_authorizer(props)
    if authorizer:
        update_kwargs["authorizerConfiguration"] = authorizer
        logger.info(
            "Set CUSTOM_JWT authorizer (allowedClients=%s)",
            props.get("AllowedClients"),
        )

    # requestHeaderConfiguration — full-replace too. Precedence:
    #   1. AllowlistedHeaders prop supplied (authoritative, e.g. chat-query runtimes
    #      forwarding the caller JWT) -> use it.
    #   2. else the live runtime already has one -> echo it (don't wipe).
    #   3. else omit.
    allowlisted_headers = props.get("AllowlistedHeaders")
    if allowlisted_headers:
        update_kwargs["requestHeaderConfiguration"] = {
            "requestHeaderAllowlist": allowlisted_headers
        }
    elif r.get("requestHeaderConfiguration"):
        update_kwargs["requestHeaderConfiguration"] = r["requestHeaderConfiguration"]

    # Other full-replace OPTIONAL fields: echo back whatever the live runtime
    # carries so the update does not narrow it. These are conditional because
    # update_agent_runtime rejects empty/None values for fields the runtime lacks.
    for key in (
        "protocolConfiguration",
        "lifecycleConfiguration",
        "metadataConfiguration",
        "description",
    ):
        if r.get(key):
            update_kwargs[key] = r[key]

    resp = client.update_agent_runtime(**update_kwargs)
    logger.info("update_agent_runtime status: %s", resp.get("status"))

    return {"PhysicalResourceId": physical_id}
