"""Custom-resource handler that drains AgentCore Runtime ENIs from a
shared security group on stack delete.

Background
----------
``AWS::BedrockAgentCore::Runtime`` resources attach AWS-managed ENIs
(``ela-attach-*``) to the runtime's VPC security group. When the stack
is deleted, CFN issues ``DeleteRuntime`` on each runtime and waits for
``DELETE_COMPLETE`` from the Bedrock control plane, but the AgentCore
service takes 30-90 seconds *after* that to fully detach its ENIs.
CFN does not wait — it proceeds to delete the SecurityGroup, which then
fails with ``DependencyViolation: has a dependent object``.

This handler runs only on ``Delete``. It polls
``DescribeNetworkInterfaces`` for ENIs still attached to the SG and waits
up to ``timeout_s`` for them to drain naturally. Any unattached ENIs that
are still associated with the SG are deleted directly. Once the SG has
zero ENIs the handler returns success and CFN proceeds with SG deletion.

``Create``/``Update`` are no-ops — the resource exists purely so that
CFN sequences ``Delete`` between the runtimes (which depend on the
drainer indirectly via the SG) and the SG itself.
"""

from __future__ import annotations

import os
import time
from typing import Any

import boto3


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """CFN custom-resource entry point.

    :param event: Standard CFN custom-resource event. ``ResourceProperties.SecurityGroupId``
        identifies the security group to drain.
    :returns: ``{"PhysicalResourceId": ...}`` so CFN can track the resource.
    """
    request_type = event['RequestType']
    physical_id = event.get('PhysicalResourceId') or 'agentcore-sg-eni-drainer'

    if request_type in ('Create', 'Update'):
        return {'PhysicalResourceId': physical_id}

    # Delete path — drain the SG.
    sg_id: str = event['ResourceProperties']['SecurityGroupId']
    timeout_s: int = int(event['ResourceProperties'].get('TimeoutSeconds', '300'))
    poll_interval_s: int = int(event['ResourceProperties'].get('PollIntervalSeconds', '10'))

    ec2 = boto3.client('ec2')
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        resp = ec2.describe_network_interfaces(
            Filters=[{'Name': 'group-id', 'Values': [sg_id]}],
        )
        enis = resp.get('NetworkInterfaces', [])
        if not enis:
            print(f'SG {sg_id} drained — 0 ENIs remaining')
            return {'PhysicalResourceId': physical_id}

        # Try to delete any ENI that is no longer attached to anything.
        # Anything still in-use is a service-managed ENI we wait on.
        in_use = []
        for eni in enis:
            eni_id = eni['NetworkInterfaceId']
            status = eni.get('Status')
            if status == 'available':
                try:
                    ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                    print(f'Deleted unattached ENI {eni_id}')
                except Exception as exc:
                    # Tolerate races where another caller deletes first.
                    print(f'Best-effort delete of {eni_id} failed: {exc}')
            else:
                in_use.append(f'{eni_id}({status})')

        if in_use:
            print(f'SG {sg_id} still has {len(in_use)} ENI(s): {in_use} — waiting {poll_interval_s}s')
        time.sleep(poll_interval_s)  # nosemgrep: arbitrary-sleep — intentional ENI drain poll interval

    # Timed out. Surface a clear error so the operator knows what to clean up.
    raise RuntimeError(
        f'Timed out after {timeout_s}s waiting for ENIs to drain from SG {sg_id}. '
        'Manual cleanup may be required: detach/delete remaining ENIs and re-issue the delete.'
    )
