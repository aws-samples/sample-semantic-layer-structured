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
"""

import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _replace_kv(csv: str, key: str, value: str) -> str:
    """Replace or append a key=value pair in a comma-separated string."""
    prefix = f"{key}="
    parts = [p for p in csv.split(",") if not p.startswith(prefix)]
    parts.append(f"{prefix}{value}")
    return ",".join(parts)


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

    env = dict(r.get("environmentVariables", {}))

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

    resp = client.update_agent_runtime(
        agentRuntimeId=runtime_id,
        environmentVariables=env,
        roleArn=r["roleArn"],
        agentRuntimeArtifact=r["agentRuntimeArtifact"],
        networkConfiguration=r["networkConfiguration"],
    )
    logger.info("update_agent_runtime status: %s", resp.get("status"))

    return {"PhysicalResourceId": physical_id}
