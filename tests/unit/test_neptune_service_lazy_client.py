"""The Neptune gateway client is built lazily, keeping the agent MCP stack off
the REST API's Lambda init path.

`strands.tools.mcp` plus `mcp_proxy_for_aws` cost ~744 ms of a ~2.04 s cold start
locally — 36% — and `neptune_api` constructs `NeptuneService` at module scope, so
before this change every cold start paid them whether or not the request touched
a Neptune route. That pushed init past Lambda's 10 s budget, which made the first
request after each container deploy fail with a 503. See
docs/plans/2026-08-25-rest-api-init-budget-design.md.
"""
import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

# `services.*` lives under the REST-API package root, not the repo root.
REST_API_ROOT = os.path.join(
    os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'
)
sys.path.insert(0, REST_API_ROOT)


def test_importing_neptune_service_does_not_import_the_agent_mcp_stack() -> None:
    """The actual regression guard: the heavy imports stay out of module scope.

    Run in a subprocess because another test in the same session may already have
    imported `strands` for its own reasons, which would mask the regression.
    """
    probe = (
        'import sys; import services.neptune_service; '
        "heavy = [m for m in sys.modules "
        "if m == 'strands' or m.startswith('strands.') "
        "or m.startswith('mcp_proxy_for_aws')]; "
        'print(",".join(sorted(heavy)))'
    )
    result = subprocess.run(
        [sys.executable, '-c', probe],
        cwd=os.path.abspath(REST_API_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f'probe failed: {result.stderr}'
    assert result.stdout.strip() == '', (
        'importing services.neptune_service pulled the agent MCP stack onto the '
        f'Lambda init path: {result.stdout.strip()}'
    )


def test_constructing_the_service_does_not_build_a_client(monkeypatch) -> None:
    """Construction must stay cheap — `neptune_api` does it at module scope."""
    monkeypatch.setenv('NEPTUNE_GATEWAY_URL', 'https://gw.example/mcp')
    from services.neptune_service import NeptuneService

    service = NeptuneService()

    # Reading the private attribute on purpose: the whole point is that nothing
    # was built yet, and touching the public property would build it.
    assert service._gateway_client is None
    assert service.neptune_gateway_url == 'https://gw.example/mcp'


def test_client_is_built_once_and_cached(monkeypatch) -> None:
    """Callers do ``with self.gateway_client:`` then call methods on it inside the
    block, so a per-access rebuild would use a session that was never entered."""
    monkeypatch.setenv('NEPTUNE_GATEWAY_URL', 'https://gw.example/mcp')
    from services.neptune_service import NeptuneService

    service = NeptuneService()
    first = service.gateway_client
    second = service.gateway_client

    assert first is not None
    assert first is second


def test_no_gateway_url_yields_none_and_callers_fail_loudly(monkeypatch) -> None:
    """The pre-existing contract: absent config is a falsy client, and the call
    sites raise rather than silently returning empty results."""
    monkeypatch.delenv('NEPTUNE_GATEWAY_URL', raising=False)
    from services.neptune_service import NeptuneService

    service = NeptuneService()

    assert service.gateway_client is None
    with pytest.raises(ValueError):
        service.list_available_tools()


def test_an_assigned_client_overrides_the_lazy_build(monkeypatch) -> None:
    """`gateway_client` stays an ordinary read/write attribute, so existing
    callers that inject a stub keep working without knowing it became a
    property."""
    monkeypatch.delenv('NEPTUNE_GATEWAY_URL', raising=False)
    from services.neptune_service import NeptuneService

    service = NeptuneService()
    stub = MagicMock()
    service.gateway_client = stub

    # Wins even though the environment says "not configured".
    assert service.gateway_client is stub


def test_the_class_default_supports_instances_built_without_init() -> None:
    """Tests build the service with `__new__` to inject a stub; the property must
    not require `__init__` to have run first."""
    from services.neptune_service import NeptuneService

    service = NeptuneService.__new__(NeptuneService)
    stub = MagicMock()
    service.gateway_client = stub

    assert service.gateway_client is stub
