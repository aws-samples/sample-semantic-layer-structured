"""Custom-resource handler for the AgentCore Memory resource backing the
lessons-learned feature (item #2).

The bedrock-agentcore-control plane exposes ``CreateMemory`` /
``DeleteMemory`` but no first-class CDK L1 yet, so we drive it through a
custom resource. The resource owns:

  - one Memory with ``eventExpiryDuration`` (short-term retention) of 90 days
  - one ``SemanticStrategy`` with namespace template
    ``/lessons/{actorId}/{sessionId}/``. Callers encode the actor as
    ``<semanticLayerId>/<semanticLayerVersion>/<userId>`` (slashes are allowed
    by the actorId regex), so the resolved namespace is
    ``/lessons/<semanticLayerId>/<semanticLayerVersion>/<userId>/<sessionId>/``
    — scoping long-term records per layer, per layer-version, per user, per
    chat session. Pinning the version isolates lessons across schema revisions.

``CreateMemory`` is idempotent on the resource name; on ``Update`` we
return the existing PhysicalResourceId when the name is unchanged.
"""

from __future__ import annotations

import time

import boto3


def _memory_id_from_name(client, name: str):
    """Find an existing memory by ``name``; return its id or ``None``."""
    paginator = client.get_paginator('list_memories')
    for page in paginator.paginate():
        for mem in page.get('memories', []) or []:
            if mem.get('name') == name:
                return mem.get('id') or mem.get('memoryId')
    return None


def _wait_active(client, memory_id: str, timeout_s: int = 300) -> None:
    """Poll until the memory reaches ACTIVE (or raise on FAILED)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = client.get_memory(memoryId=memory_id)
        status = (resp.get('memory') or {}).get('status') or resp.get('status')
        if status == 'ACTIVE':
            return
        if status == 'FAILED':
            raise RuntimeError(f"memory {memory_id} entered FAILED state")
        time.sleep(5)  # nosemgrep: arbitrary-sleep — intentional memory ACTIVE status poll
    raise TimeoutError(
        f"memory {memory_id} did not reach ACTIVE within {timeout_s}s"
    )


def _create_memory(client, *, name: str, expiry_days: int):
    """Create the memory + a semantic-strategy. Returns the memory id.

    The strategy is created in the same call so we don't have to deal with
    a second control-plane round-trip from the handler.
    """
    response = client.create_memory(
        name=name,
        description='Lessons-learned long-term memory for the semantic layer',
        eventExpiryDuration=expiry_days,
        memoryStrategies=[
            {
                'semanticMemoryStrategy': {
                    'name': 'lessons_semantic',
                    # actorId encodes "<semanticLayerId>/<semanticLayerVersion>/<userId>"
                    # so the resolved namespace becomes
                    # /lessons/<semanticLayerId>/<semanticLayerVersion>/<userId>/<sessionId>/
                    'namespaces': ['/lessons/{actorId}/{sessionId}/'],
                }
            }
        ],
    )
    memory = response.get('memory') or {}
    memory_id = memory.get('id') or memory.get('memoryId')
    if not memory_id:
        raise RuntimeError(
            f"create_memory returned no id; raw response: {response!r}"
        )
    _wait_active(client, memory_id)
    return memory_id


def on_event(event, context):
    """Custom-resource entry point — handles Create/Update/Delete."""
    request_type = event['RequestType']
    props = event['ResourceProperties']
    name = props['MemoryName']
    expiry_days = int(props.get('EventExpiryDays', 90))

    client = boto3.client('bedrock-agentcore-control')

    if request_type == 'Create':
        existing = _memory_id_from_name(client, name)
        if existing:
            # Re-adopt — eg. previous deploy was rolled back after success.
            return {
                'PhysicalResourceId': existing,
                'Data': {'MemoryId': existing},
            }
        memory_id = _create_memory(
            client, name=name, expiry_days=expiry_days
        )
        return {
            'PhysicalResourceId': memory_id,
            'Data': {'MemoryId': memory_id},
        }

    if request_type == 'Update':
        # Name change -> CFN gives us a fresh PhysicalResourceId by issuing
        # a Delete on the old one. Anything else is a no-op (the memory's
        # mutable surface — strategies, expiry — would need a separate
        # ``update_memory`` call; not supported until callers ask for it).
        return {
            'PhysicalResourceId': event['PhysicalResourceId'],
            'Data': {'MemoryId': event['PhysicalResourceId']},
        }

    if request_type == 'Delete':
        memory_id = event['PhysicalResourceId']
        try:
            client.delete_memory(memoryId=memory_id)
        except Exception:  # nosec B110 — best-effort cleanup/telemetry; failure must not break the request path
            # Idempotent — already-gone or never-created is fine.
            pass
        return {'PhysicalResourceId': memory_id}

    raise ValueError(f"unsupported request type: {request_type}")
