"""IAM tests for the AgentCore evaluation stack.

CreateEvaluator synchronously validates that the CALLER identity (the
EvalConfigHandler Lambda role making the API call) can invoke the judge model.
If that role lacks bedrock:InvokeModel on the global judge inference-profile,
CreateEvaluator fails with a ValidationException at deploy time.
"""

import json
import os
import subprocess

import pytest

# Shells out to `npx cdk synth` — needs Node + cdk/node_modules. Deselected by
# the python-only CI job (`-m 'not requires_node'`); runs locally / where Node
# is present. See pyproject.toml [tool.pytest.ini_options] markers.
pytestmark = pytest.mark.requires_node

_TEMPLATE = '/tmp/cdk-eval-synth/semantic-layer-dev-agentcore-eval.template.json'  # nosemgrep: hardcoded-tmp-path — CDK -o output dir; not user-controlled input


def _synth() -> dict:
    """Synthesize the agentcore-eval stack and return the parsed template.

    :returns: the parsed CloudFormation template as a dict.
    """
    cdk = os.path.join(os.path.dirname(__file__), '..', '..', 'cdk')
    acct = os.environ.get('CDK_DEFAULT_ACCOUNT', '000000000000')
    out = subprocess.run(
        [
            'npx',
            'cdk',
            'synth',
            'semantic-layer-dev-agentcore-eval',
            '--quiet',
            '-o',
            '/tmp/cdk-eval-synth',  # nosemgrep: hardcoded-tmp-path — CDK synth output dir passed as CLI arg; not user-controlled input
        ],
        cwd=cdk,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            'CDK_DEFAULT_ACCOUNT': acct,
            'CDK_DEFAULT_REGION': 'us-east-1',
            'AWS_REGION': 'us-east-1',
        },
    )
    assert out.returncode == 0, out.stderr[-3000:]
    with open(_TEMPLATE, encoding='utf-8') as f:
        return json.load(f)


def _statements_with_invoke_model(policy_doc: dict) -> list[dict]:
    """Return statements in a PolicyDocument that allow bedrock:InvokeModel.

    :param policy_doc: an IAM PolicyDocument dict.
    :returns: list of matching statement dicts.
    """
    matches: list[dict] = []
    for stmt in policy_doc.get('Statement', []):
        actions = stmt.get('Action', [])
        if isinstance(actions, str):
            actions = [actions]
        if 'bedrock:InvokeModel' in actions:
            matches.append(stmt)
    return matches


def test_eval_config_handler_role_can_invoke_judge_model() -> None:
    """The EvalConfigHandler (caller) role must grant bedrock:InvokeModel on the
    global judge inference-profile, else CreateEvaluator fails ValidationException.

    Asserts the grant is attached to a policy whose Roles reference a logical id
    containing 'EvalConfigHandler' — not merely present somewhere in the template.
    """
    tpl = _synth()
    resources = tpl['Resources']

    # Collect every IAM::Policy whose document grants bedrock:InvokeModel,
    # paired with the logical ids of the roles it attaches to.
    invoke_policies: list[tuple[dict, list[str]]] = []
    for res in resources.values():
        if res['Type'] != 'AWS::IAM::Policy':
            continue
        doc = res['Properties']['PolicyDocument']
        stmts = _statements_with_invoke_model(doc)
        if not stmts:
            continue
        role_logical_ids: list[str] = []
        for role_ref in res['Properties'].get('Roles', []):
            # Roles are { "Ref": "<LogicalId>" }
            if isinstance(role_ref, dict) and 'Ref' in role_ref:
                role_logical_ids.append(role_ref['Ref'])
        invoke_policies.append((stmts[0], role_logical_ids))

    # Two distinct InvokeModel grants are expected: caller (EvalConfigHandler)
    # and execution (EvalExecutionRole) roles.
    assert len(invoke_policies) >= 2, (
        f'expected >=2 InvokeModel grants (caller + execution), '
        f'found {len(invoke_policies)}'
    )

    # The caller-role grant is the whole point of this fix: find an InvokeModel
    # policy attached to the EvalConfigHandler role.
    caller_grants = [
        stmt
        for stmt, role_ids in invoke_policies
        if any('EvalConfigHandler' in rid for rid in role_ids)
    ]
    assert caller_grants, (
        'no bedrock:InvokeModel grant is attached to the EvalConfigHandler '
        '(caller) role; CreateEvaluator will fail ValidationException'
    )

    # That grant must cover the global judge inference-profile with an
    # unpinned region (a global. cross-region profile is not region-bound).
    caller_resources = json.dumps(caller_grants[0]['Resource'])
    assert 'inference-profile/global.anthropic' in caller_resources
    assert 'bedrock:*::foundation-model/anthropic' in caller_resources
