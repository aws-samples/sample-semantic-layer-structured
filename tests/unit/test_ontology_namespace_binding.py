"""Unit tests for the ontology -> namespace binding.

``namespaceId`` on an ontology config row is what lets the authorization
middleware turn ``/ontology/config/{id}`` — a path with no namespace in it — into
a namespace-scoped decision. Two properties matter and neither is self-evident
from the code: an ontology cannot be moved between namespaces by re-saving its
config, and an ontology with no binding stays unbound rather than silently
acquiring the default.
"""
import os
import sys
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api')
)

from services.ontology_service import (  # noqa: E402
    OntologyService,
    default_namespace_id,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the ambient default-namespace configuration.

    Args:
        monkeypatch: pytest's environment patcher.

    Returns:
        None.
    """
    monkeypatch.delenv('DEFAULT_NAMESPACE_ID', raising=False)


def build_service(*, existing: Dict[str, Any] | None = None) -> OntologyService:
    """Build an OntologyService with a mocked table and no AWS dependencies.

    Args:
        existing: The config ``get_metadata_config`` should report, or None.

    Returns:
        The service, with ``table.put_item`` recording the written item.
    """
    svc = OntologyService.__new__(OntologyService)
    svc.table = MagicMock()
    svc.get_metadata_config = MagicMock(return_value=existing)  # type: ignore[method-assign]
    return svc


def written_item(svc: OntologyService) -> Dict[str, Any]:
    """Return the item the service put to DynamoDB.

    Args:
        svc: The service whose table was mocked.

    Returns:
        The written item.
    """
    return svc.table.put_item.call_args.kwargs['Item']


# ── Default namespace configuration ───────────────────────────────────


def test_default_is_empty_when_unset() -> None:
    """No namespace registry means no binding, not a guessed one."""
    assert default_namespace_id() == ''


def test_default_is_read_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configuration update must take effect without a new container."""
    monkeypatch.setenv('DEFAULT_NAMESPACE_ID', 'ns-1')
    assert default_namespace_id() == 'ns-1'


# ── Binding on create ─────────────────────────────────────────────────


def test_explicit_namespace_is_stored() -> None:
    """A caller-supplied namespace binds the new ontology."""
    svc = build_service()
    config = svc.create_metadata_config({'name': 'sales', 'namespaceId': 'ns-7'})
    assert config['namespaceId'] == 'ns-7'
    assert written_item(svc)['namespaceId'] == 'ns-7'


def test_default_namespace_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing single-tenant deployments keep working after the bootstrap seeds one."""
    monkeypatch.setenv('DEFAULT_NAMESPACE_ID', 'ns-default')
    svc = build_service()
    assert svc.create_metadata_config({'name': 'sales'})['namespaceId'] == (
        'ns-default'
    )


def test_no_binding_when_nothing_is_configured() -> None:
    """The key is absent rather than empty, so an unbound ontology is denied.

    Writing ``''`` would make the resolver return a falsy namespace that still
    looks like a value to anything doing a `has namespaceId` check.
    """
    svc = build_service()
    config = svc.create_metadata_config({'name': 'sales'})
    assert 'namespaceId' not in config


# ── Binding on update ─────────────────────────────────────────────────


def test_existing_binding_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-saving a config must not move the ontology to the default namespace."""
    monkeypatch.setenv('DEFAULT_NAMESPACE_ID', 'ns-default')
    svc = build_service(
        existing={'id': 'ont-1', 'version': 'v2', 'namespaceId': 'ns-7'}
    )
    config = svc.create_metadata_config({'id': 'ont-1', 'name': 'sales'})
    assert config['namespaceId'] == 'ns-7'


def test_existing_binding_wins_over_the_request_body() -> None:
    """An ontology must not be re-homed by a config save.

    Moving it would transfer it out from under the grants that govern it — a
    privilege change disguised as an edit. A deliberate move belongs behind an
    explicit, separately-authorized operation.
    """
    svc = build_service(
        existing={'id': 'ont-1', 'version': 'v2', 'namespaceId': 'ns-7'}
    )
    config = svc.create_metadata_config(
        {'id': 'ont-1', 'name': 'sales', 'namespaceId': 'ns-attacker'}
    )
    assert config['namespaceId'] == 'ns-7'


def test_unbound_existing_config_can_be_bound() -> None:
    """Backfilling a legacy ontology's namespace must be possible."""
    svc = build_service(existing={'id': 'ont-1', 'version': 'v1'})
    config = svc.create_metadata_config(
        {'id': 'ont-1', 'name': 'sales', 'namespaceId': 'ns-7'}
    )
    assert config['namespaceId'] == 'ns-7'
