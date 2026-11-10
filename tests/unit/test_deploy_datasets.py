"""Unit tests for the resumable deploy-driver's PURE core.

Covers the ``DeployState`` resumable-state model (save/load round-trip,
``mark_done``/``is_done``), the ``pending_dbs`` planning helper, the pure
``_role_arn_from_caller_identity`` LF-principal derivation, and the agentcore
role auto-discovery (``match_agent_roles`` / ``discover_agent_role_arns``). The
CLI ``main()`` (boto3 orchestration) is intentionally NOT unit-tested here — it
composes these pure pieces with the already-reviewed ingest helpers + AWS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from scripts.benchmarks.deploy_datasets import (
    _AGENT_ROLE_NAME_PARTS,
    _AGENTCORE_PRINCIPAL,
    _LAMBDA_PRINCIPAL,
    DeployState,
    _role_arn_from_caller_identity,
    discover_agent_role_arns,
    match_agent_roles,
    pending_dbs,
    trusted_services,
)


def _trust(*services: str) -> Dict[str, Any]:
    """Build a minimal IAM trust policy document naming the given services.

    Args:
        services: Service principals to allow sts:AssumeRole for.

    Returns:
        An ``AssumeRolePolicyDocument``-shaped dict.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": list(services)},
                "Action": "sts:AssumeRole",
            }
        ],
    }


def test_deploy_state_roundtrip(tmp_path: Path):
    # A saved state must reload identically so a re-run knows what completed.
    state = DeployState(benchmark="bird", completed=["superhero", "california_schools"])
    p = tmp_path / "deploy_state.json"
    state.save(path=p)
    back = DeployState.load(path=p)
    assert back.benchmark == "bird"
    assert back.completed == ["superhero", "california_schools"]


def test_deploy_state_load_creates_parent_dirs(tmp_path: Path):
    # save() must create missing parent directories (state file lives under a
    # nested data/eval/... path that may not exist yet).
    p = tmp_path / "nested" / "dir" / "deploy_state.json"
    DeployState(benchmark="cwd").save(path=p)
    assert p.exists()


def test_mark_done_and_is_done():
    state = DeployState(benchmark="bird")
    assert state.is_done(db_id="superhero") is False
    state.mark_done(db_id="superhero")
    assert state.is_done(db_id="superhero") is True


def test_mark_done_is_idempotent():
    # Marking the same db twice must not duplicate it (a crashed+resumed run may
    # re-mark a db that was already recorded).
    state = DeployState(benchmark="bird")
    state.mark_done(db_id="superhero")
    state.mark_done(db_id="superhero")
    assert state.completed == ["superhero"]


def test_pending_dbs_filters_completed():
    state = DeployState(benchmark="bird", completed=["b"])
    # Order is preserved and completed ids are dropped.
    assert pending_dbs(all_db_ids=["a", "b", "c"], state=state) == ["a", "c"]


def test_pending_dbs_empty_state_is_all_pending():
    state = DeployState(benchmark="bird")
    assert pending_dbs(all_db_ids=["a", "b", "c"], state=state) == ["a", "b", "c"]


def test_pending_dbs_all_done_is_none():
    state = DeployState(benchmark="bird", completed=["a", "b", "c"])
    assert pending_dbs(all_db_ids=["a", "b", "c"], state=state) == []


# _role_arn_from_caller_identity: a wrong result makes every LF grant target a
# nonexistent principal, so pin the assumed-role -> role/NAME conversion.
def test_role_arn_from_assumed_role():
    arn = _role_arn_from_caller_identity(
        arn="arn:aws:sts::123456789012:assumed-role/MyRole/session-abc"
    )
    assert arn == "arn:aws:iam::123456789012:role/MyRole"


def test_role_arn_passthrough_plain_role():
    arn = _role_arn_from_caller_identity(arn="arn:aws:iam::123456789012:role/MyRole")
    assert arn == "arn:aws:iam::123456789012:role/MyRole"


def test_role_arn_unrecognized_raises():
    with pytest.raises(ValueError):
        _role_arn_from_caller_identity(arn="arn:aws:iam::123456789012:user/bob")


# --------------------------------------------------------------------------- #
# Agentcore role auto-discovery.                                              #
#                                                                             #
# These exist because a hand-maintained grant list once omitted the ontology   #
# BUILD role, and that omission does NOT fail loudly: Phase 1's                #
# get_single_table_schema returns {"error": ...} on AccessDenied without       #
# raising, so the agent modelled tables from the LLM's memory and still        #
# reported status='completed'. 10 of 11 BIRD layers were guesses (2026-08-04). #
# --------------------------------------------------------------------------- #
# One realistic role name per part, with the CDK's random suffix, mapped to the
# trust policy the real role carries. The three `SemanticLayerCreationAgen` decoys
# are REAL sibling roles from the same stack: CloudFormation truncates the logical
# id to 25 chars, so the hybrid creation runtime role and its container-build roles
# are indistinguishable by name and only the trust policy separates them.
_FAKE_ROLES: Dict[str, Dict[str, Any]] = {
    "semantic-layer-dev-agentc-OntologyGenerationAgentRo-f0miAcVh87P9": _trust(
        _AGENTCORE_PRINCIPAL
    ),
    "semantic-layer-dev-agentc-MetadataAgentRole0A1C1619-Pi4WcNazu9Vt": _trust(
        _AGENTCORE_PRINCIPAL
    ),
    "semantic-layer-dev-agentc-SemanticQueryAgentRole2FA-Q9KCaUP08ZcP": _trust(
        _AGENTCORE_PRINCIPAL
    ),
    "semantic-layer-dev-agentc-SemanticLayerQueryAgentRo-pTdh4Ql9dSjA": _trust(
        _AGENTCORE_PRINCIPAL
    ),
    "semantic-layer-dev-agentc-SemanticLayerCreationAgen-YnCeDba6irg9": _trust(
        _AGENTCORE_PRINCIPAL
    ),
    "semantic-layer-dev-agentc-MetadataQueryAgentRoleB4A-PHxCyZ9fWzbW": _trust(
        _AGENTCORE_PRINCIPAL
    ),
    "semantic-layer-dev-agentc-BenchmarkEvalHandlerRewar-wKzqLJ5vJM0p": _trust(
        _LAMBDA_PRINCIPAL
    ),
    "semantic-layer-dev-agentc-BenchmarkEvalHandlerExecu-PwWUA4ZPq53K": _trust(
        _LAMBDA_PRINCIPAL
    ),
    "semantic-layer-dev-agentc-BenchmarkEvalHandlerSoftF-5KJmKEicoVx4": _trust(
        _LAMBDA_PRINCIPAL
    ),
    # Same truncated name as the creation RUNTIME role — must NOT be matched.
    "semantic-layer-dev-agentc-SemanticLayerCreationAgen-bakHNJfPHnY3": _trust(
        "codebuild.amazonaws.com"
    ),
    "semantic-layer-dev-agentc-SemanticLayerCreationAgen-msyx1ma9YuwS": _trust(
        _LAMBDA_PRINCIPAL
    ),
    "semantic-layer-dev-agentc-SemanticLayerCreationAgen-jUaLU42CKlOj": _trust(
        "states.amazonaws.com"
    ),
    # REST API Lambda — the governed-rules publish gate smoke-executes SQL from
    # this role, so it needs LF SELECT like any query role.
    "semantic-layer-dev-lambda-LambdaExecutionRoleD5C260-vwLoQNFKkxRf": _trust(
        _LAMBDA_PRINCIPAL
    ),
    "some-unrelated-role": _trust("ec2.amazonaws.com"),          # must NOT be matched
    "AWSServiceRoleForSupport": _trust("support.amazonaws.com"),  # must NOT be matched
}

# The same account as {role name: trusted services} — what match_agent_roles takes.
_FAKE_ROLE_TRUSTS: Dict[str, List[str]] = {
    name: trusted_services(assume_role_policy=policy)
    for name, policy in _FAKE_ROLES.items()
}


class _FakeIam:
    """Minimal boto3 ``iam`` stand-in exposing only the paginator we use."""

    def __init__(self, *, roles: Dict[str, Dict[str, Any]]) -> None:
        """Store the roles this fake account contains.

        Parameters:
            roles: ``{role name: AssumeRolePolicyDocument}`` to return from
                list_roles.
        Returns:
            None.
        """
        self._roles = roles

    def get_paginator(self, operation: str) -> Any:
        """Return a paginator whose paginate() yields one page of roles.

        Parameters:
            operation: Must be 'list_roles'; anything else is a test bug.
        Returns:
            An object with a paginate() method.
        """
        assert operation == "list_roles", f"unexpected paginator {operation!r}"
        pages = [
            {
                "Roles": [
                    {"RoleName": name, "AssumeRolePolicyDocument": policy}
                    for name, policy in self._roles.items()
                ]
            }
        ]

        class _Paginator:
            def paginate(self) -> List[Dict[str, Any]]:
                """Yield the single canned page of roles."""
                return pages

        return _Paginator()


# trusted_services: a parse miss makes EVERY part match nothing, which aborts the
# deploy — loud, but it must not happen on well-formed input.
def test_trusted_services_accepts_list_and_bare_string():
    assert trusted_services(assume_role_policy=_trust("a.amazonaws.com")) == [
        "a.amazonaws.com"
    ]
    bare = {"Statement": [{"Principal": {"Service": "b.amazonaws.com"}}]}
    assert trusted_services(assume_role_policy=bare) == ["b.amazonaws.com"]


def test_trusted_services_accepts_url_encoded_json_string():
    # boto3 pre-decodes the document; the raw API/CLI returns URL-encoded JSON.
    encoded = "%7B%22Statement%22%3A%5B%7B%22Principal%22%3A%7B%22Service%22%3A%22c.amazonaws.com%22%7D%7D%5D%7D"
    assert trusted_services(assume_role_policy=encoded) == ["c.amazonaws.com"]


def test_trusted_services_tolerates_missing_or_principal_free_policy():
    assert trusted_services(assume_role_policy=None) == []
    # Trusted by an IAM principal rather than a service — no Service key at all.
    iam_only = {"Statement": [{"Principal": {"AWS": "arn:aws:iam::1:root"}}]}
    assert trusted_services(assume_role_policy=iam_only) == []


def test_match_agent_roles_matches_suffixed_cdk_names():
    # CDK appends a random suffix, so matching must be by SUBSTRING, not equality.
    matched = match_agent_roles(roles=_FAKE_ROLE_TRUSTS)
    assert set(matched) == {part for part, _trust_svc in _AGENT_ROLE_NAME_PARTS}
    assert matched["OntologyGenerationAgentRo"] == [
        "semantic-layer-dev-agentc-OntologyGenerationAgentRo-f0miAcVh87P9"
    ]
    # All three eval Lambdas share one part, so that part must collect all three.
    assert len(matched["BenchmarkEvalHandler"]) == 3
    # The REST API Lambda runs the rules publish gate's smoke query.
    assert matched["lambda-LambdaExecutionRole"] == [
        "semantic-layer-dev-lambda-LambdaExecutionRoleD5C260-vwLoQNFKkxRf"
    ]


def test_match_agent_roles_excludes_unrelated_roles():
    matched = match_agent_roles(roles=_FAKE_ROLE_TRUSTS)
    all_hits = {name for hits in matched.values() for name in hits}
    assert "some-unrelated-role" not in all_hits
    assert "AWSServiceRoleForSupport" not in all_hits


def test_match_agent_roles_picks_runtime_role_over_same_named_build_roles():
    # THE 2026-08-14 regression: the hybrid creation RUNTIME role and its three
    # container-build roles share a truncated name. Only the bedrock-agentcore
    # trust policy may match, or CodeBuild/Lambda/Step Functions service roles
    # would be granted SELECT on every benchmark table.
    matched = match_agent_roles(roles=_FAKE_ROLE_TRUSTS)
    assert matched["SemanticLayerCreationAgen"] == [
        "semantic-layer-dev-agentc-SemanticLayerCreationAgen-YnCeDba6irg9"
    ]


def test_match_agent_roles_reports_empty_for_absent_part():
    # An absent part must surface as an empty list, not be silently dropped —
    # that emptiness is what discover_agent_role_arns turns into a hard failure.
    matched = match_agent_roles(
        roles={"only-a-query-role-SemanticQueryAgentRole-x": [_AGENTCORE_PRINCIPAL]}
    )
    assert matched["SemanticQueryAgentRole"]
    assert matched["OntologyGenerationAgentRo"] == []


def test_match_agent_roles_rejects_name_match_with_wrong_trust():
    # Right name, wrong trust service -> no match (and therefore a loud failure
    # upstream) rather than a grant to the wrong principal.
    matched = match_agent_roles(
        roles={"x-OntologyGenerationAgentRo-y": ["codebuild.amazonaws.com"]}
    )
    assert matched["OntologyGenerationAgentRo"] == []


def test_discover_agent_role_arns_returns_all_ten():
    arns = discover_agent_role_arns(
        iam=_FakeIam(roles=_FAKE_ROLES), account_id="123456789012"
    )
    # 6 agentcore runtime roles + 3 eval Lambdas + the REST API Lambda (whose
    # rules publish gate smoke-executes SQL); the 3 build-role decoys and the
    # 2 unrelated roles must be excluded.
    assert len(arns) == 10
    assert all(a.startswith("arn:aws:iam::123456789012:role/") for a in arns)
    assert any("OntologyGenerationAgentRo" in a for a in arns)
    assert any("SemanticLayerCreationAgen-YnCeDba6irg9" in a for a in arns)
    assert any("lambda-LambdaExecutionRole" in a for a in arns)
    assert not any("SemanticLayerCreationAgen-bakHNJfPHnY3" in a for a in arns)


def test_discover_agent_role_arns_raises_when_ontology_role_missing():
    # THE regression under test: an incomplete grant set must abort the deploy
    # rather than produce ontologies modelled from the LLM's memory.
    without_ontology = {
        name: policy
        for name, policy in _FAKE_ROLES.items()
        if "OntologyGenerationAgentRo" not in name
    }
    with pytest.raises(RuntimeError, match="OntologyGenerationAgentRo"):
        discover_agent_role_arns(
            iam=_FakeIam(roles=without_ontology), account_id="123456789012"
        )


def test_discover_agent_role_arns_raises_when_only_build_roles_match():
    # The hybrid creation role's build siblings exist but the runtime role does
    # not: that must abort, not quietly grant the build roles instead.
    build_only = {
        name: policy
        for name, policy in _FAKE_ROLES.items()
        if name != "semantic-layer-dev-agentc-SemanticLayerCreationAgen-YnCeDba6irg9"
    }
    with pytest.raises(RuntimeError, match="SemanticLayerCreationAgen"):
        discover_agent_role_arns(
            iam=_FakeIam(roles=build_only), account_id="123456789012"
        )


def test_discover_agent_role_arns_raises_on_empty_account():
    with pytest.raises(RuntimeError, match="agentcore role part"):
        discover_agent_role_arns(iam=_FakeIam(roles={}), account_id="123456789012")
