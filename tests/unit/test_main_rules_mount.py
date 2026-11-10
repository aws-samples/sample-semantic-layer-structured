"""The /rules router mounts only when both env vars are present.

`main.py` does its mounting at module scope, so the only way to exercise the
conditional is to re-import the module under a controlled environment.
"""
import importlib
import os
import sys

import pytest

# `main`, `routers.*` and `services.*` live under the REST-API package root,
# which is not on sys.path by default (only the repo root is, via pyproject's
# `pythonpath`).
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'),
)


def _reload_main(monkeypatch, **env):
    """Reimport main.py with a controlled environment.

    :param monkeypatch: pytest's env patcher; reverted after the test.
    :param env: Environment variables to set before the import.
    :returns: The freshly imported `main` module.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("main", None)
    return importlib.import_module("main")


def _paths(app) -> set:
    """Collect every registered route path on a FastAPI app.

    Read from the OpenAPI schema rather than walking `app.routes`, because a
    router added with `include_router` is stored as an `_IncludedRouter` whose
    prefix is composed lazily — it has no `.path` at all. The schema is what a
    client actually sees, and it is stable across FastAPI versions.

    :param app: The FastAPI application.
    :returns: Set of fully-prefixed route path strings.
    """
    return set(app.openapi()["paths"].keys())


def test_rules_router_is_absent_when_the_flag_is_off(monkeypatch) -> None:
    main = _reload_main(monkeypatch, RULES_ENABLED="false",
                        NEPTUNE_GATEWAY_URL="https://gw")
    assert not any("/rules" in p for p in _paths(main.app))


def test_rules_router_is_absent_without_a_gateway_url(monkeypatch) -> None:
    """Rules live in Neptune — mounting without the gateway would 500 on
    every call, which is worse than a clean 404."""
    main = _reload_main(monkeypatch, RULES_ENABLED="true",
                        NEPTUNE_GATEWAY_URL="")
    assert not any("/rules" in p for p in _paths(main.app))


def test_rules_router_mounts_when_enabled(monkeypatch) -> None:
    main = _reload_main(monkeypatch, RULES_ENABLED="true",
                        NEPTUNE_GATEWAY_URL="https://gw")
    assert any("/rules/namespaces/{ns}/rules" in p for p in _paths(main.app))


def test_the_publish_route_is_registered(monkeypatch) -> None:
    main = _reload_main(monkeypatch, RULES_ENABLED="true",
                        NEPTUNE_GATEWAY_URL="https://gw")
    assert any(p.endswith("{rule_id}:publish") for p in _paths(main.app))


def test_the_threshold_approval_route_is_registered(monkeypatch) -> None:
    main = _reload_main(monkeypatch, RULES_ENABLED="true",
                        NEPTUNE_GATEWAY_URL="https://gw")
    assert any("thresholds/{threshold_id}:approve" in p for p in _paths(main.app))


@pytest.fixture(autouse=True)
def _restore_main():
    """Leave sys.modules clean so later tests get the default app."""
    yield
    sys.modules.pop("main", None)
