"""Unit tests for grant resolution and the deny-wins restriction merge.

The two invariants under test are both fail-open hazards: dropping a page of
grants, and letting an unrestricted grant dissolve a restrictive one.
"""
from typing import Any, Dict, List

import pytest

from agents.shared.grant_profile import (
    GLOBAL_RESOURCE_ID,
    ResolvedProfile,
    build_principal_keys,
    resolve_profile,
    sanitize_principal_key,
)


class FakeTable:
    """Minimal stand-in for a boto3 DynamoDB Table that paginates.

    ``pages`` maps a principal key to the list of pages returned for it, so a
    test can prove the resolver follows ``LastEvaluatedKey``.
    """

    def __init__(self, pages: Dict[str, List[List[Dict[str, Any]]]]) -> None:
        """Store the canned pages and initialise the call log.

        Args:
            pages: principal key -> list of pages, each a list of grant items.
        """
        self._pages = pages
        self.query_calls: List[Dict[str, Any]] = []

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        """Return the page addressed by ``ExclusiveStartKey``, if any.

        Args:
            **kwargs: The boto3 ``Query`` arguments.

        Returns:
            A response dict with ``Items`` and, when more pages remain, a
            ``LastEvaluatedKey``.
        """
        self.query_calls.append(kwargs)
        # The KeyConditionExpression is a boto3 condition object; its values
        # are reachable via the private tuple, so read the key from the repr-safe
        # `_values` attribute instead of re-implementing condition parsing.
        condition = kwargs['KeyConditionExpression']
        principal_key = condition._values[1]
        pages = self._pages.get(principal_key, [[]])
        index = int(kwargs.get('ExclusiveStartKey', {}).get('page', 0))
        items = pages[index] if index < len(pages) else []
        response: Dict[str, Any] = {'Items': items}
        if index + 1 < len(pages):
            response['LastEvaluatedKey'] = {'page': index + 1}
        return response


class ExplodingTable:
    """A table whose query always fails, to prove the resolver re-raises."""

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        """Always raise.

        Args:
            **kwargs: Ignored.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError('DynamoDB unavailable')


# ── Principal keys ────────────────────────────────────────────────────


def test_sanitize_lowercases_email_but_not_sub() -> None:
    """Emails normalise case; opaque subs and group names do not."""
    assert sanitize_principal_key(value='Alice@Example.com') == 'alice@example.com'
    assert sanitize_principal_key(value='AbC-123') == 'AbC-123'
    assert sanitize_principal_key(value='Data Stewards') == 'Data%20Stewards'


def test_sanitize_encodes_key_breaking_characters() -> None:
    """``+``, ``#`` and ``/`` would corrupt the ``<type>::<id>`` layout."""
    encoded = sanitize_principal_key(value='foo+123@example.com')
    assert '+' not in encoded
    assert encoded == 'foo%2B123@example.com'


def test_build_principal_keys_covers_sub_email_and_groups() -> None:
    """Every identifier the caller presents is queried."""
    keys = build_principal_keys(
        user_id='sub-1', email='a@b.com', groups=['Admin', 'Users']
    )
    assert keys == ['User::sub-1', 'User::a@b.com', 'Group::Admin', 'Group::Users']


def test_build_principal_keys_deduplicates() -> None:
    """A sub equal to the email collapses to one key."""
    keys = build_principal_keys(user_id='a@b.com', email='a@b.com', groups=[])
    assert keys == ['User::a@b.com']


def test_build_principal_keys_skips_empty_identifiers() -> None:
    """An absent email claim contributes no key."""
    assert build_principal_keys(user_id='sub-1', email='', groups=['']) == [
        'User::sub-1'
    ]


# ── Role resolution ───────────────────────────────────────────────────


def test_global_and_resource_roles_are_separated() -> None:
    """``resourceId == GLOBAL`` becomes a platform role; anything else is scoped."""
    table = FakeTable(
        {
            'User::sub-1': [
                [
                    {'role': 'platform-admin', 'resourceId': GLOBAL_RESOURCE_ID},
                    {'role': 'namespace-owner', 'resourceId': 'ns-1'},
                ]
            ]
        }
    )
    profile = resolve_profile(user_id='sub-1', table=table)
    assert profile.global_roles == ['platform-admin']
    assert profile.resource_roles == [
        {'role': 'namespace-owner', 'resourceUID': 'ns-1'}
    ]


def test_resolution_follows_every_page() -> None:
    """A restrictive grant on page 2 must not be dropped.

    Without full pagination the merge below sees only the unrestricted grant and
    the firewall stops enforcing the denylist.
    """
    table = FakeTable(
        {
            'User::sub-1': [
                [{'role': 'namespace-owner', 'resourceId': 'ns-1'}],
                [
                    {
                        'role': 'namespace-data-analyst',
                        'resourceId': 'ns-1',
                        'columnDenylist': {'parties': ['ssn']},
                    }
                ],
            ]
        }
    )
    profile = resolve_profile(user_id='sub-1', namespace='ns-1', table=table)
    assert profile.column_denylist == {'parties': ['ssn']}


def test_lookup_failure_reraises() -> None:
    """A partial read would be more permissive than the truth, so it must not happen."""
    with pytest.raises(RuntimeError, match='DynamoDB unavailable'):
        resolve_profile(user_id='sub-1', table=ExplodingTable())


def test_group_grants_are_resolved() -> None:
    """A grant written against a Cognito group resolves for its members."""
    table = FakeTable(
        {'Group::Data Stewards': [[{'role': 'namespace-data-steward', 'resourceId': 'ns-1'}]]}
    )
    # The group key is URL-encoded on the way in, so encode the fixture key too.
    table._pages['Group::Data%20Stewards'] = table._pages.pop('Group::Data Stewards')
    profile = resolve_profile(user_id='sub-1', groups=['Data Stewards'], table=table)
    assert profile.resource_roles == [
        {'role': 'namespace-data-steward', 'resourceUID': 'ns-1'}
    ]


def test_no_grants_table_configured_returns_identity_only(monkeypatch: Any) -> None:
    """Without GRANTS_TABLE there are no grants to find; identity still resolves."""
    monkeypatch.delenv('GRANTS_TABLE', raising=False)
    profile = resolve_profile(user_id='sub-1', groups=['Admin'])
    assert profile.user_id == 'sub-1'
    assert profile.groups == ['Admin']
    assert profile.global_roles == []


# ── Deny-wins merge ───────────────────────────────────────────────────


def test_unrestricted_grant_does_not_dissolve_a_denylist() -> None:
    """The core deny-wins property.

    Adding ``namespace-owner`` (no restrictions) to an analyst carrying a PII
    denylist must not stop that denylist being enforced.
    """
    table = FakeTable(
        {
            'User::sub-1': [
                [
                    {
                        'role': 'namespace-data-analyst',
                        'resourceId': 'ns-1',
                        'columnDenylist': {'parties': ['ssn']},
                        'tableAllowlist': ['parties'],
                    },
                    {'role': 'namespace-owner', 'resourceId': 'ns-1'},
                ]
            ]
        }
    )
    profile = resolve_profile(user_id='sub-1', namespace='ns-1', table=table)
    assert profile.column_denylist == {'parties': ['ssn']}
    assert profile.table_allowlist == ['parties']


def test_denylists_union_across_grants() -> None:
    """A column is denied if ANY grant denies it."""
    table = FakeTable(
        {
            'User::sub-1': [
                [
                    {
                        'role': 'r1',
                        'resourceId': 'ns-1',
                        'columnDenylist': {'parties': ['ssn']},
                    },
                    {
                        'role': 'r2',
                        'resourceId': 'ns-1',
                        'columnDenylist': {'parties': ['dob'], 'claims': ['amount']},
                    },
                ]
            ]
        }
    )
    profile = resolve_profile(user_id='sub-1', namespace='ns-1', table=table)
    assert profile.column_denylist == {'claims': ['amount'], 'parties': ['dob', 'ssn']}


def test_allowlists_union_across_declaring_grants() -> None:
    """Union, not intersection — the principal was plainly granted each table."""
    table = FakeTable(
        {
            'User::sub-1': [
                [
                    {'role': 'r1', 'resourceId': 'ns-1', 'tableAllowlist': ['a']},
                    {'role': 'r2', 'resourceId': 'ns-1', 'tableAllowlist': ['b']},
                ]
            ]
        }
    )
    profile = resolve_profile(user_id='sub-1', namespace='ns-1', table=table)
    assert profile.table_allowlist == ['a', 'b']


def test_no_declared_allowlist_means_unrestricted() -> None:
    """Absence stays absence — the firewall must see None, not an empty list."""
    table = FakeTable({'User::sub-1': [[{'role': 'r1', 'resourceId': 'ns-1'}]]})
    profile = resolve_profile(user_id='sub-1', namespace='ns-1', table=table)
    assert profile.table_allowlist is None
    assert profile.column_denylist is None


def test_explicit_empty_allowlist_is_honoured() -> None:
    """An administrator's deny-all is a declaration, unlike absence."""
    table = FakeTable(
        {'User::sub-1': [[{'role': 'r1', 'resourceId': 'ns-1', 'tableAllowlist': []}]]}
    )
    profile = resolve_profile(user_id='sub-1', namespace='ns-1', table=table)
    assert profile.table_allowlist == []


def test_global_grants_excluded_from_the_data_merge() -> None:
    """Platform privileges flow through global_roles, not through restrictions."""
    table = FakeTable(
        {
            'User::sub-1': [
                [
                    {
                        'role': 'platform-admin',
                        'resourceId': GLOBAL_RESOURCE_ID,
                        'tableAllowlist': ['everything'],
                    },
                    {
                        'role': 'namespace-data-analyst',
                        'resourceId': 'ns-1',
                        'tableAllowlist': ['parties'],
                    },
                ]
            ]
        }
    )
    profile = resolve_profile(user_id='sub-1', namespace='ns-1', table=table)
    assert profile.table_allowlist == ['parties']


def test_restrictions_scoped_to_the_target_namespace() -> None:
    """A grant on another namespace contributes nothing."""
    table = FakeTable(
        {
            'User::sub-1': [
                [
                    {
                        'role': 'r1',
                        'resourceId': 'ns-2',
                        'columnDenylist': {'parties': ['ssn']},
                    }
                ]
            ]
        }
    )
    profile = resolve_profile(user_id='sub-1', namespace='ns-1', table=table)
    assert profile.column_denylist is None


def test_allowed_metrics_union_of_declaring_grants() -> None:
    """``allowedMetrics`` merges with the same declared-union rule."""
    table = FakeTable(
        {
            'User::sub-1': [
                [
                    {'role': 'r1', 'resourceId': 'ns-1', 'allowedMetrics': ['m1']},
                    {'role': 'r2', 'resourceId': 'ns-1'},
                ]
            ]
        }
    )
    profile = resolve_profile(user_id='sub-1', namespace='ns-1', table=table)
    assert profile.allowed_metrics == ['m1']


# ── inject_into ───────────────────────────────────────────────────────


def test_inject_into_omits_absent_restrictions() -> None:
    """None must not become a key, or the firewall loses absent-vs-empty."""
    profile: Dict[str, Any] = {}
    ResolvedProfile(user_id='sub-1').inject_into(profile=profile)
    assert profile == {'userId': 'sub-1'}


def test_inject_into_carries_empty_allowlist() -> None:
    """An explicit deny-all must survive injection."""
    profile: Dict[str, Any] = {}
    ResolvedProfile(user_id='sub-1', table_allowlist=[]).inject_into(profile=profile)
    assert profile['tableAllowlist'] == []
