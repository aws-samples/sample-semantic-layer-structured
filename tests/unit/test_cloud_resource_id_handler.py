"""Unit tests for the consolidated CloudResourceId custom-resource handler.

This handler is the SINGLE authoritative ``update_agent_runtime`` (full-replace)
caller for each AgentCore Runtime. It restores env (BaseEnvironmentVariables),
layers the cloud.resource_id / eval-log-group OTEL patches on top, AND re-applies
the CUSTOM_JWT authorizer + request-header allowlist — so a container-only redeploy
can no longer wipe the authorizer (the regression that motivated folding the
authorizer in here). boto3 is mocked so no AWS calls are made.
"""

import importlib.util
import os

from unittest.mock import MagicMock, patch

# Load the handler by file path under a UNIQUE module name — it is named index.py
# inside a bundled Lambda asset dir, the same bare name as other handlers under
# test, so a plain `import index` would collide in sys.modules. See the sibling
# test_runtime_authorizer_handler.py note (now removed) for the original rationale.
_HANDLER_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..',
        '..',
        'cdk',
        'lib',
        'stacks',
        'backend',
        'agentcore-cloud-resource-id-handler',
        'index.py',
    )
)
_spec = importlib.util.spec_from_file_location('cloud_resource_id_index', _HANDLER_PATH)
index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index)


# ── _build_authorizer ────────────────────────────────────────────────────────


def test_build_authorizer_maps_discovery_and_clients():
    """_build_authorizer maps DiscoveryUrl + AllowedClients to the JWT shape."""
    payload = index._build_authorizer(
        {'DiscoveryUrl': 'https://disc/.well-known/openid-configuration', 'AllowedClients': ['spa', 'm2m']}
    )
    assert payload == {
        'customJWTAuthorizer': {
            'discoveryUrl': 'https://disc/.well-known/openid-configuration',
            'allowedClients': ['spa', 'm2m'],
        }
    }


def test_build_authorizer_returns_none_when_inputs_absent():
    """No DiscoveryUrl / AllowedClients → None (runtime keeps IAM inbound auth)."""
    assert index._build_authorizer({}) is None
    assert index._build_authorizer({'DiscoveryUrl': 'd'}) is None
    assert index._build_authorizer({'DiscoveryUrl': 'd', 'AllowedClients': []}) is None


# ── on_event full-replace payload ────────────────────────────────────────────


def _run_on_event(props, current_runtime):
    """Invoke on_event with boto3 mocked, return the update_agent_runtime kwargs.

    :param props: the ResourceProperties dict for the CFN event.
    :param current_runtime: the dict get_agent_runtime should return.
    :returns: the kwargs passed to update_agent_runtime.
    """
    with patch.object(index, 'boto3') as boto3_mock:
        agentcore_client = MagicMock()
        logs_client = MagicMock()

        # boto3.client('logs') vs boto3.client('bedrock-agentcore-control')
        def _client(service, **_kwargs):
            return logs_client if service == 'logs' else agentcore_client

        boto3_mock.client.side_effect = _client
        agentcore_client.get_agent_runtime.return_value = current_runtime

        index.on_event(
            {'RequestType': 'Create', 'ResourceProperties': props},
            None,
        )
        return agentcore_client.update_agent_runtime.call_args.kwargs


_RUNTIME_ARN = 'arn:aws:bedrock-agentcore:us-east-1:1:runtime/rt-abc'
_BASE_CURRENT = {
    'roleArn': 'role-arn',
    'networkConfiguration': {'net': 1},
    'agentRuntimeArtifact': {'art': 1},
}


def test_on_event_reapplies_authorizer_from_props():
    """The full-replace update re-sends the CUSTOM_JWT authorizer from CFN props,
    so a container redeploy (which re-fires this resource) cannot wipe it."""
    kw = _run_on_event(
        props={
            'AgentRuntimeArn': _RUNTIME_ARN,
            'Region': 'us-east-1',
            'BaseEnvironmentVariables': {'OTEL_RESOURCE_ATTRIBUTES': 'service.name=x'},
            'DiscoveryUrl': 'https://disc/.well-known/openid-configuration',
            'AllowedClients': ['spa', 'm2m'],
            'AllowlistedHeaders': ['Authorization'],
        },
        current_runtime=dict(_BASE_CURRENT),
    )
    assert kw['authorizerConfiguration'] == {
        'customJWTAuthorizer': {
            'discoveryUrl': 'https://disc/.well-known/openid-configuration',
            'allowedClients': ['spa', 'm2m'],
        }
    }
    assert kw['requestHeaderConfiguration'] == {'requestHeaderAllowlist': ['Authorization']}
    # Required full-replace fields echoed back.
    assert kw['roleArn'] == 'role-arn'
    assert kw['agentRuntimeArtifact'] == {'art': 1}
    assert kw['networkConfiguration'] == {'net': 1}


def test_on_event_omits_authorizer_for_iam_fallback_runtime():
    """No authorizer props (IAM-fallback runtime / no pool) → the update omits
    authorizerConfiguration entirely rather than sending an empty one."""
    kw = _run_on_event(
        props={
            'AgentRuntimeArn': _RUNTIME_ARN,
            'Region': 'us-east-1',
            'BaseEnvironmentVariables': {'OTEL_RESOURCE_ATTRIBUTES': 'service.name=x'},
        },
        current_runtime=dict(_BASE_CURRENT),
    )
    assert 'authorizerConfiguration' not in kw
    assert 'requestHeaderConfiguration' not in kw


def test_on_event_restores_base_env_and_patches_otel():
    """env comes from BaseEnvironmentVariables (authoritative), with cloud.resource_id
    and the eval log-group layered on — not from the live runtime env."""
    kw = _run_on_event(
        props={
            'AgentRuntimeArn': _RUNTIME_ARN,
            'Region': 'us-east-1',
            'BaseEnvironmentVariables': {
                'OTEL_RESOURCE_ATTRIBUTES': 'service.name=x',
                'OTEL_EXPORTER_OTLP_LOGS_HEADERS': 'x-aws-log-stream=runtime-logs',
                'ATHENA_WORKGROUP': 'wg',
            },
        },
        current_runtime={**_BASE_CURRENT, 'environmentVariables': {'STALE': 'should-be-ignored'}},
    )
    env = kw['environmentVariables']
    # Base env preserved; stale live env NOT used.
    assert env['ATHENA_WORKGROUP'] == 'wg'
    assert 'STALE' not in env
    # cloud.resource_id patched with the runtime ARN + trailing slash.
    assert f'cloud.resource_id={_RUNTIME_ARN}/' in env['OTEL_RESOURCE_ATTRIBUTES']
    # eval log-group redirect applied to both attributes + headers.
    assert 'aws.log.group.names=/aws/bedrock-agentcore/runtimes/rt-abc-DEFAULT' in env['OTEL_RESOURCE_ATTRIBUTES']
    assert 'x-aws-log-group=/aws/bedrock-agentcore/runtimes/rt-abc-DEFAULT' in env['OTEL_EXPORTER_OTLP_LOGS_HEADERS']


def test_on_event_preserves_optional_config_when_present():
    """Other full-replace optional fields present on the live runtime are echoed
    back so the consolidated update does not narrow the runtime."""
    kw = _run_on_event(
        props={
            'AgentRuntimeArn': _RUNTIME_ARN,
            'Region': 'us-east-1',
            'BaseEnvironmentVariables': {'OTEL_RESOURCE_ATTRIBUTES': 'service.name=x'},
        },
        current_runtime={
            **_BASE_CURRENT,
            'lifecycleConfiguration': {'idleRuntimeSessionTimeout': 900},
            'protocolConfiguration': {'serverProtocol': 'HTTP'},
            'description': 'my runtime',
        },
    )
    assert kw['lifecycleConfiguration'] == {'idleRuntimeSessionTimeout': 900}
    assert kw['protocolConfiguration'] == {'serverProtocol': 'HTTP'}
    assert kw['description'] == 'my runtime'


def test_on_event_preserves_live_headers_when_prop_absent():
    """When AllowlistedHeaders is not supplied but the runtime already has a
    requestHeaderConfiguration, echo the live value (full-replace would wipe it)."""
    kw = _run_on_event(
        props={
            'AgentRuntimeArn': _RUNTIME_ARN,
            'Region': 'us-east-1',
            'BaseEnvironmentVariables': {'OTEL_RESOURCE_ATTRIBUTES': 'service.name=x'},
            'DiscoveryUrl': 'd',
            'AllowedClients': ['spa'],
        },
        current_runtime={
            **_BASE_CURRENT,
            'requestHeaderConfiguration': {'requestHeaderAllowlist': ['Authorization', 'X-Trace-Id']},
        },
    )
    assert kw['requestHeaderConfiguration'] == {
        'requestHeaderAllowlist': ['Authorization', 'X-Trace-Id']
    }


def test_on_event_delete_is_noop():
    """Delete neither reads nor updates the runtime — just echoes a physical id."""
    with patch.object(index, 'boto3') as boto3_mock:
        result = index.on_event(
            {
                'RequestType': 'Delete',
                'ResourceProperties': {'AgentRuntimeArn': _RUNTIME_ARN, 'Region': 'us-east-1'},
            },
            None,
        )
        boto3_mock.client.assert_not_called()
    assert result['PhysicalResourceId'] == 'cloud-resource-id-rt-abc'
