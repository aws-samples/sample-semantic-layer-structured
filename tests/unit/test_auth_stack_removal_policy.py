import json
import os
import subprocess

import pytest

# Shells out to `npx cdk synth` — needs Node + cdk/node_modules. Deselected by
# the python-only CI job (`-m 'not requires_node'`); runs locally / where Node
# is present. See pyproject.toml [tool.pytest.ini_options] markers.
pytestmark = pytest.mark.requires_node


def test_user_pool_deletes_by_default():
    """Synthesized UserPool must have DeletionPolicy Delete unless retainUserPool=true."""
    cdk = os.path.join(os.path.dirname(__file__), '..', '..', 'cdk')
    acct = os.environ.get('CDK_DEFAULT_ACCOUNT', '000000000000')
    out = subprocess.run(
        ['npx','cdk','synth','semantic-layer-dev-auth','--quiet','-o','/tmp/cdk-auth-synth'],  # nosemgrep: hardcoded-tmp-path — CDK synth output dir; not user-controlled input
        cwd=cdk, capture_output=True, text=True,
        env={**os.environ,'CDK_DEFAULT_ACCOUNT':acct,'CDK_DEFAULT_REGION':'us-east-1','AWS_REGION':'us-east-1'})
    assert out.returncode == 0, out.stderr[-3000:]
    with open('/tmp/cdk-auth-synth/semantic-layer-dev-auth.template.json', encoding='utf-8') as f:  # nosemgrep: hardcoded-tmp-path — reading CDK synth output; not user-controlled input
        tpl = json.load(f)
    pools = [r for r in tpl['Resources'].values() if r['Type']=='AWS::Cognito::UserPool']
    assert pools, 'no UserPool in template'
    assert all(p.get('DeletionPolicy')=='Delete' for p in pools), \
        [p.get('DeletionPolicy') for p in pools]
