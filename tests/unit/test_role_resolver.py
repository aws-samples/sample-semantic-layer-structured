"""Unit tests for role resolution: group mapping, caching, and fail-loud lookups.

The two hazards guarded here are (a) a grants-table failure degrading to "this
principal has no roles", which is indistinguishable from a legitimate denial, and
(b) the resolution cache serving one principal's roles to another.
"""
import os
import sys
from typing import Any, Dict, List

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api')
)

from authz import role_resolver  # noqa: E402
from authz.cedar_schema import Role  # noqa: E402
from authz.role_resolver import (  # noqa: E402
    ResolvedRoles,
    RoleResolutionError,
    clear_cache,
    group_role_map,
    resolve_roles,
    roles_from_groups,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear authz configuration and the resolution cache before each test.

    Args:
        monkeypatch: pytest's environment patcher.

    Returns:
        None.
    """
    monkeypatch.delenv('GRANTS_TABLE', raising=False)
    monkeypatch.delenv('AUTHZ_GROUP_ROLE_MAP', raising=False)
    monkeypatch.delenv('AUTHZ_CACHE_TTL_SECONDS', raising=False)
    clear_cache()


# ── Group mapping ─────────────────────────────────────────────────────


def test_default_map_covers_both_spellings_of_each_group() -> None:
    """Two stacks create the groups under different names; all four must map.

    ``auth/index.ts`` creates Admin/Users and ``lf-groups/index.ts`` creates
    Admins/QueryUsers. Mapping only one pair would leave half the existing users
    with no roles when enforcement is switched on.
    """
    mapping = group_role_map()
    assert mapping['Admin'] == Role.PLATFORM_ADMIN.value
    assert mapping['Admins'] == Role.PLATFORM_ADMIN.value
    assert mapping['Users'] == Role.PLATFORM_VIEWER.value
    assert mapping['QueryUsers'] == Role.PLATFORM_VIEWER.value


def test_env_override_replaces_the_default_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The override replaces rather than extends, so a group can be retired."""
    monkeypatch.setenv('AUTHZ_GROUP_ROLE_MAP', '{"Analysts": "platform-viewer"}')
    mapping = group_role_map()
    assert mapping == {'Analysts': 'platform-viewer'}
    assert 'Admin' not in mapping


@pytest.mark.parametrize('raw', ['{not json', '[]', '{"Admin": 1}'])
def test_malformed_map_raises_rather_than_falling_back(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Silently restoring the default would grant roles an operator removed."""
    monkeypatch.setenv('AUTHZ_GROUP_ROLE_MAP', raw)
    with pytest.raises(RoleResolutionError):
        group_role_map()


def test_unmapped_groups_contribute_nothing() -> None:
    """Lake Formation groups exist for unrelated reasons and grant no roles."""
    assert roles_from_groups(groups=['SomeOtherGroup']) == []


def test_group_roles_are_deduplicated() -> None:
    """Admin and Admins both map to platform-admin; the role appears once."""
    assert roles_from_groups(groups=['Admin', 'Admins']) == [
        Role.PLATFORM_ADMIN.value
    ]


# ── Resolution without a grants table ─────────────────────────────────


def test_groups_alone_produce_global_roles() -> None:
    """Bootstrap path: no grants exist yet, so groups are the only source."""
    resolved = resolve_roles(user_id='u1', groups=['Admin'])
    assert resolved.global_roles == [Role.PLATFORM_ADMIN.value]
    assert resolved.resource_roles == []


def test_groups_cannot_grant_a_namespace_role() -> None:
    """A group name carries no namespace, so it can only ever be platform-scoped."""
    resolved = resolve_roles(user_id='u1', groups=['Admin'], namespace='ns-1')
    assert resolved.resource_roles == []


def test_empty_user_id_is_rejected() -> None:
    """An empty subject means the request never passed the JWT authorizer."""
    with pytest.raises(ValueError, match='non-empty user_id'):
        resolve_roles(user_id='')


# ── Fail-loud grants lookup ───────────────────────────────────────────


def test_grants_failure_raises_rather_than_returning_no_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"No grants found" and "could not read grants" must not look the same.

    Degrading to an empty role set would present as a 403 for every legitimate
    write, which is a silent outage rather than a reported one.
    """

    def explode(**kwargs: Any) -> None:
        """Stand in for a failing DynamoDB read.

        Args:
            **kwargs: Ignored.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError('DynamoDB unavailable')

    monkeypatch.setattr(role_resolver, 'resolve_profile', explode)
    with pytest.raises(RoleResolutionError, match='grants lookup failed'):
        resolve_roles(user_id='u1', groups=['Admin'])


def test_grant_global_roles_merge_with_group_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both sources contribute; neither replaces the other."""
    from agents.shared.grant_profile import ResolvedProfile

    monkeypatch.setattr(
        role_resolver,
        'resolve_profile',
        lambda **kwargs: ResolvedProfile(
            user_id='u1', global_roles=[Role.PLATFORM_VIEWER.value]
        ),
    )
    resolved = resolve_roles(user_id='u1', groups=['Admin'])
    assert resolved.global_roles == [
        Role.PLATFORM_ADMIN.value,
        Role.PLATFORM_VIEWER.value,
    ]


def test_resource_roles_and_restrictions_pass_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Data restrictions resolved from grants reach the firewall profile."""
    from agents.shared.grant_profile import ResolvedProfile

    monkeypatch.setattr(
        role_resolver,
        'resolve_profile',
        lambda **kwargs: ResolvedProfile(
            user_id='u1',
            resource_roles=[{'role': 'namespace-data-analyst', 'resourceUID': 'ns-1'}],
            column_denylist={'parties': ['ssn']},
        ),
    )
    resolved = resolve_roles(user_id='u1', namespace='ns-1')
    assert resolved.resource_roles[0]['resourceUID'] == 'ns-1'
    assert resolved.to_grant_profile()['columnDenylist'] == {'parties': ['ssn']}


# ── Grant profile rendering ───────────────────────────────────────────


def test_grant_profile_omits_absent_restrictions() -> None:
    """None must not become a key: the firewall distinguishes absent from empty."""
    profile = ResolvedRoles(user_id='u1').to_grant_profile()
    assert profile == {'userId': 'u1'}


def test_grant_profile_carries_an_explicit_deny_all() -> None:
    """An empty allowlist is a declaration and must survive rendering."""
    profile = ResolvedRoles(user_id='u1', table_allowlist=[]).to_grant_profile()
    assert profile['tableAllowlist'] == []


def test_role_names_deduplicates_across_scopes() -> None:
    """Policy selection needs each role once, whatever scope granted it."""
    resolved = ResolvedRoles(
        user_id='u1',
        global_roles=[Role.PLATFORM_VIEWER.value],
        resource_roles=[
            {'role': Role.PLATFORM_VIEWER.value, 'resourceUID': 'ns-1'},
            {'role': Role.NAMESPACE_OWNER.value, 'resourceUID': 'ns-1'},
        ],
    )
    assert resolved.role_names() == [
        Role.PLATFORM_VIEWER.value,
        Role.NAMESPACE_OWNER.value,
    ]


# ── Cache ─────────────────────────────────────────────────────────────


def _counting_resolver(calls: List[Dict[str, Any]]) -> Any:
    """Build a resolve_profile stub that records its calls.

    Args:
        calls: List to append each call's kwargs to.

    Returns:
        The stub callable.
    """
    from agents.shared.grant_profile import ResolvedProfile

    def stub(**kwargs: Any) -> ResolvedProfile:
        """Record the call and return an empty profile.

        Args:
            **kwargs: The resolve_profile arguments.

        Returns:
            An identity-only profile.
        """
        calls.append(kwargs)
        return ResolvedProfile(user_id=str(kwargs.get('user_id') or ''))

    return stub


def test_repeat_resolution_is_served_from_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One API call triggers several decisions; they share a single grants read."""
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(role_resolver, 'resolve_profile', _counting_resolver(calls))
    resolve_roles(user_id='u1', namespace='ns-1')
    resolve_roles(user_id='u1', namespace='ns-1')
    assert len(calls) == 1


def test_cache_is_keyed_on_the_full_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One principal's roles must never be served to another.

    Every identity component participates in the key, including the namespace —
    restrictions are merged per namespace, so a shared key would leak them.
    """
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(role_resolver, 'resolve_profile', _counting_resolver(calls))
    resolve_roles(user_id='u1', namespace='ns-1')
    resolve_roles(user_id='u2', namespace='ns-1')
    resolve_roles(user_id='u1', namespace='ns-2')
    resolve_roles(user_id='u1', email='u1@example.com', namespace='ns-1')
    resolve_roles(user_id='u1', groups=['Admin'], namespace='ns-1')
    assert len(calls) == 5


def test_clear_cache_forces_a_fresh_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A grant change must take effect immediately, not after the TTL."""
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(role_resolver, 'resolve_profile', _counting_resolver(calls))
    resolve_roles(user_id='u1')
    clear_cache()
    resolve_roles(user_id='u1')
    assert len(calls) == 2


def test_use_cache_false_bypasses_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that must see current grants can opt out."""
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(role_resolver, 'resolve_profile', _counting_resolver(calls))
    resolve_roles(user_id='u1')
    resolve_roles(user_id='u1', use_cache=False)
    assert len(calls) == 2


def test_zero_ttl_disables_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator can turn the stale-role window off entirely."""
    monkeypatch.setenv('AUTHZ_CACHE_TTL_SECONDS', '0')
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(role_resolver, 'resolve_profile', _counting_resolver(calls))
    resolve_roles(user_id='u1')
    resolve_roles(user_id='u1')
    assert len(calls) == 2


def test_expired_entry_is_re_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TTL is enforced against the monotonic clock, not merely stored."""
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(role_resolver, 'resolve_profile', _counting_resolver(calls))
    clock = {'now': 1000.0}
    monkeypatch.setattr(role_resolver.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setenv('AUTHZ_CACHE_TTL_SECONDS', '30')
    resolve_roles(user_id='u1')
    clock['now'] += 31.0
    resolve_roles(user_id='u1')
    assert len(calls) == 2


def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded cache on a long-lived Lambda container is a memory leak."""
    monkeypatch.setattr(role_resolver, 'resolve_profile', _counting_resolver([]))
    for index in range(role_resolver._CACHE_MAX_ENTRIES + 50):
        resolve_roles(user_id=f'u{index}')
    assert len(role_resolver._cache) <= role_resolver._CACHE_MAX_ENTRIES


@pytest.mark.parametrize('raw', ['abc', '-1'])
def test_malformed_ttl_raises(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """A typo must not silently re-enable a longer stale-role window."""
    monkeypatch.setenv('AUTHZ_CACHE_TTL_SECONDS', raw)
    with pytest.raises(RoleResolutionError):
        resolve_roles(user_id='u1')
