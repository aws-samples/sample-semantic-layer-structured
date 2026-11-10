"""The SQL firewall is actually WIRED at every user-influenced execution site.

``tests/unit/test_sql_firewall.py`` covers the firewall's own decisions. This
file covers the thing that decides whether any of that matters: whether each
site that submits SQL to Athena calls it, and whether a denial reaches the
caller in the shape that site's caller can act on.

The five wired sites, and why each returns-vs-raises differently:

============================================================  ==============
site                                                          on denial
============================================================  ==============
``agents.shared.metric_executor.execute_metric``               raises
``agents.semantic_layer_query_agent.metric_executor``          raises
``agents.semantic_layer_query_agent.execution_agent``          error dict
``agents.metadata_query_agent.main.execute_sql_query``         tool result
``lambda/rest-api/main.py::_execute_athena_sql``               raises
============================================================  ==============

The ``@tool`` site MUST return rather than raise: a Strands ``@tool`` that
raises has its exception handed back to the model as a tool result and retried,
so raising there loops the agent forever (see
``tests/unit/test_strands_tool_error_contract.py`` for the general rule). The
executor site returns because ``BoundedSqlExecutor`` short-circuits on ``error``
and surfaces it verbatim as the answer, which is how the reason reaches the
user.

The REST API site is asserted structurally rather than behaviourally:
``_execute_athena_sql`` is a closure defined inside
``if _RULES_ENABLED and _NEPTUNE_GATEWAY_URL:`` at module scope, so it cannot be
imported without a configured Neptune gateway. The AST check below still catches
the regression that matters — the firewall call being removed, or moved after
``start_query_execution``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from agents.metadata_query_agent import main as metadata_main
from agents.ontology_query_agent import main as ontology_main
from agents.semantic_layer_query_agent import execution_agent as hybrid_execution
from agents.semantic_layer_query_agent import firewall_context as hybrid_ctx
from agents.semantic_layer_query_agent import metric_executor as hybrid_metrics
from agents.semantic_layer_query_agent.sql_firewall import (
    UnsafeSQLError as HybridUnsafeSQLError,
)
from agents.shared import firewall_context as shared_ctx
from agents.shared.metric_executor import execute_metric
from agents.shared.sql_firewall import UnsafeSQLError

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The raw function behind the @tool — same unwrap as test_execute_sql_retry.py.
_execute_sql_query = (
    getattr(metadata_main.execute_sql_query, "_tool_func", None)
    or getattr(metadata_main.execute_sql_query, "__wrapped__", None)
    or metadata_main.execute_sql_query
)

# Two statements that must be refused everywhere, whatever the profile.
# The CTE form is the interesting one: its root node is a Select, so only the
# firewall's deep AST walk catches it.
_DDL = "DROP TABLE customers"
_DML_IN_CTE = "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d"


@pytest.fixture(autouse=True)
def _unbound_by_default():
    """Clear both firewall-context copies before and after every test.

    ContextVars survive within a pytest process, so a profile bound by one test
    would otherwise leak into the next and make the unbound-posture assertions
    pass or fail depending on ordering.

    Returns:
        None (yields control to the test).
    """
    shared_ctx.clear_caller()
    hybrid_ctx.clear_caller()
    yield
    shared_ctx.clear_caller()
    hybrid_ctx.clear_caller()


def _metric(*, sql: str, metric_id: str = "m1", dialect: str = "athena") -> MagicMock:
    """Build a duck-typed metric for the executors.

    Args:
        sql: The metric's compiled SQL.
        metric_id: The metric id ``allowedMetrics`` is checked against.
        dialect: The dialect the SQL was compiled for.

    Returns:
        A MagicMock carrying the four attributes ``execute_metric`` reads.
    """
    return MagicMock(
        metric_id=metric_id,
        compiled_sql=sql,
        dialect=dialect,
        supported_filters=[],
        supported_dimensions=[],
    )


def _succeeding_athena() -> MagicMock:
    """Return an Athena mock whose query SUCCEEDS with a single row.

    Returns:
        The mock client.
    """
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "q-1"}
    athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}},
    }
    athena.get_query_results.return_value = {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": "n"}]},
                {"Data": [{"VarCharValue": "1"}]},
            ],
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "n"}]},
        }
    }
    return athena


def _run_metric(*, executor, metric, athena) -> Dict[str, Any]:
    """Invoke one of the two ``execute_metric`` copies with fixed plumbing.

    Args:
        executor: The ``execute_metric`` callable under test.
        metric: The duck-typed metric.
        athena: The Athena client stand-in.

    Returns:
        The executor's shaped result dict.
    """
    return executor(
        metric=metric,
        filters={},
        athena=athena,
        workgroup="wg",
        output_loc="s3://bkt/",
    )


# --------------------------------------------------------------------------
# Unbound = structural checks only (the documented sparse-opt-in posture)
# --------------------------------------------------------------------------


def test_unbound_still_allows_a_plain_select():
    """No profile bound must NOT mean no firewall — a safe SELECT still runs."""
    athena = _succeeding_athena()
    out = _run_metric(
        executor=execute_metric, metric=_metric(sql="SELECT 1 AS n"), athena=athena
    )
    assert out["rows"] == [{"n": "1"}]
    athena.start_query_execution.assert_called_once()


@pytest.mark.parametrize("sql", [_DDL, _DML_IN_CTE])
def test_unbound_still_refuses_non_read_only_sql(sql: str):
    """Structural checks are always on, profile or not, and block before Athena."""
    athena = MagicMock()
    with pytest.raises(UnsafeSQLError):
        _run_metric(executor=execute_metric, metric=_metric(sql=sql), athena=athena)
    athena.start_query_execution.assert_not_called()


def test_unbound_refuses_unparseable_sql():
    """Unparseable SQL is denied with NO profile bound, not just a restrictive one.

    This is the behaviour change with the widest blast radius, so it is pinned
    here: an unparseable statement cannot be proven read-only, and the pre-parse
    regex only inspects the leading keyword. Malformed SQL therefore never
    reaches Athena, which is why ``test_vkg_run_athena_sql.py``'s FAILED-path
    test had to switch to a well-formed statement.
    """
    athena = MagicMock()
    with pytest.raises(UnsafeSQLError, match="parse"):
        _run_metric(
            executor=execute_metric, metric=_metric(sql="SELECT 1 FRM x"), athena=athena
        )
    athena.start_query_execution.assert_not_called()


@pytest.mark.parametrize("sql", [_DDL, _DML_IN_CTE])
def test_hybrid_metric_executor_refuses_the_same_statements(sql: str):
    """The VENDORED executor the hybrid agent actually imports is wired too.

    ``semantic_layer_query_agent/main.py`` imports ``.metric_executor``, not the
    shared copy, so testing only the shared one would leave the live Tier 1 path
    unverified.

    Note the exception class: vendoring gives the hybrid package its own
    ``UnsafeSQLError``, so it is NOT caught by ``except`` clauses naming the
    shared one. Anything catching a firewall denial inside this package must
    import it relatively.
    """
    athena = MagicMock()
    with pytest.raises(HybridUnsafeSQLError):
        _run_metric(
            executor=hybrid_metrics.execute_metric,
            metric=_metric(sql=sql),
            athena=athena,
        )
    athena.start_query_execution.assert_not_called()


# --------------------------------------------------------------------------
# Bound restrictions are enforced, and the reason is surfaced
# --------------------------------------------------------------------------


def test_table_allowlist_denies_an_unlisted_table_and_names_it():
    """A denial must say WHICH table, or the caller cannot act on it."""
    shared_ctx.bind_profile(profile={"tableAllowlist": ["orders"]}, namespace="ns-1")
    athena = MagicMock()
    with pytest.raises(UnsafeSQLError, match="salaries"):
        _run_metric(
            executor=execute_metric,
            metric=_metric(sql="SELECT amount FROM salaries"),
            athena=athena,
        )
    athena.start_query_execution.assert_not_called()


def test_table_allowlist_permits_a_listed_table():
    """The allowlist gates, it does not block everything."""
    shared_ctx.bind_profile(profile={"tableAllowlist": ["orders"]}, namespace="ns-1")
    athena = _succeeding_athena()
    _run_metric(
        executor=execute_metric,
        metric=_metric(sql="SELECT total AS n FROM orders"),
        athena=athena,
    )
    athena.start_query_execution.assert_called_once()


def test_column_denylist_blocks_select_star_fail_closed():
    """``SELECT *`` over a column-restricted table cannot be proven safe."""
    shared_ctx.bind_profile(
        profile={"columnDenylist": {"employees": ["ssn"]}}, namespace="ns-1"
    )
    with pytest.raises(UnsafeSQLError, match="SELECT \\*"):
        _run_metric(
            executor=execute_metric,
            metric=_metric(sql="SELECT * FROM employees"),
            athena=MagicMock(),
        )


def test_allowed_metrics_denies_an_unlisted_metric():
    """A metric is gated by identity, before its SQL is even parsed."""
    shared_ctx.bind_profile(profile={"allowedMetrics": ["revenue"]}, namespace="ns-1")
    athena = MagicMock()
    with pytest.raises(PermissionError, match="headcount"):
        _run_metric(
            executor=execute_metric,
            metric=_metric(sql="SELECT 1 AS n", metric_id="headcount"),
            athena=athena,
        )
    athena.start_query_execution.assert_not_called()


def test_allowed_metrics_permits_a_listed_metric():
    """The metric gate lets a granted metric through."""
    shared_ctx.bind_profile(profile={"allowedMetrics": ["revenue"]}, namespace="ns-1")
    athena = _succeeding_athena()
    _run_metric(
        executor=execute_metric,
        metric=_metric(sql="SELECT 1 AS n", metric_id="revenue"),
        athena=athena,
    )
    athena.start_query_execution.assert_called_once()


def test_empty_allowed_metrics_denies_every_metric():
    """An explicitly empty list is deny-all and must NOT fail open.

    ``[]`` is falsy, so a truthiness check here would permit everything — the
    exact bug the ``is None`` guards in ``check_metric_allowed`` exist to avoid.
    """
    shared_ctx.bind_profile(profile={"allowedMetrics": []}, namespace="ns-1")
    with pytest.raises(PermissionError):
        _run_metric(
            executor=execute_metric,
            metric=_metric(sql="SELECT 1 AS n"),
            athena=MagicMock(),
        )


def test_empty_table_allowlist_denies_every_table():
    """Same deny-all semantics for an explicitly empty ``tableAllowlist``."""
    shared_ctx.bind_profile(profile={"tableAllowlist": []}, namespace="ns-1")
    with pytest.raises(UnsafeSQLError):
        _run_metric(
            executor=execute_metric,
            metric=_metric(sql="SELECT total AS n FROM orders"),
            athena=MagicMock(),
        )


def test_a_profile_declaring_no_restrictions_is_not_a_denial():
    """Identity-only profiles (no grants declare restrictions) run normally."""
    shared_ctx.bind_profile(profile={"userId": "u-1"}, namespace="ns-1")
    athena = _succeeding_athena()
    _run_metric(
        executor=execute_metric,
        metric=_metric(sql="SELECT total AS n FROM orders"),
        athena=athena,
    )
    athena.start_query_execution.assert_called_once()


# --------------------------------------------------------------------------
# Per-site denial shape
# --------------------------------------------------------------------------


def _wire_metadata_agent(monkeypatch, athena_client) -> Dict[str, Any]:
    """Stub the metadata agent's session and state so ``_execute_sql_query`` runs.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        athena_client: The Athena stand-in the tool should receive.

    Returns:
        The mutable state dict the tool reads and writes, so a test can assert
        on the memo flag afterwards.
    """
    session = MagicMock()
    session.client.side_effect = lambda name, *a, **k: (
        athena_client if name == "athena" else MagicMock()
    )
    monkeypatch.setattr(metadata_main, "get_boto_session", lambda: session)
    state: Dict[str, Any] = {"query_executed": False, "cached_results": {}}
    monkeypatch.setattr(metadata_main, "_get_state", lambda: state)
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    return state


@pytest.mark.parametrize("sql", [_DDL, _DML_IN_CTE])
def test_metadata_tool_returns_the_denial_instead_of_raising(monkeypatch, sql: str):
    """A ``@tool`` denial comes back as a tool result — raising would loop."""
    athena = MagicMock()
    _wire_metadata_agent(monkeypatch, athena)
    out = json.loads(_execute_sql_query(sql, "db", "AwsDataCatalog"))
    assert "Query denied" in out["error"]
    athena.start_query_execution.assert_not_called()


def test_metadata_tool_clears_the_memo_so_a_rewrite_can_run(monkeypatch):
    """The model is expected to retry with different SQL.

    A latched ``query_executed`` would make that retry a no-op and the turn
    would end with the denial as the answer even though a permitted query
    existed.
    """
    state = _wire_metadata_agent(monkeypatch, MagicMock())
    state["query_executed"] = True
    _execute_sql_query(_DDL, "db", "AwsDataCatalog")
    assert state["query_executed"] is False


def test_metadata_tool_surfaces_the_allowlist_reason(monkeypatch):
    """The denial text names the table, so the model can rewrite the query."""
    shared_ctx.bind_profile(profile={"tableAllowlist": ["orders"]}, namespace="ns-1")
    _wire_metadata_agent(monkeypatch, MagicMock())
    out = json.loads(_execute_sql_query("SELECT x FROM salaries", "db", "cat"))
    assert "salaries" in out["error"]


@pytest.mark.parametrize("sql", [_DDL, _DML_IN_CTE])
def test_ontology_query_agent_raises_rather_than_returning_a_failure_dict(sql: str):
    """A denial must not enter Phase 5's LLM SQL-repair loop.

    ``_run_athena_sql`` returns ``{"success": False, "state_change_reason": …}``
    for Athena query failures, and Phase 5 feeds that back to an LLM to repair.
    A structural refusal is not repairable, so it raises instead of grinding.
    """
    with pytest.raises(UnsafeSQLError):
        ontology_main._run_athena_sql(
            sql=sql, database_name="db", catalog_id="AwsDataCatalog"
        )


@pytest.mark.parametrize("sql", [_DDL, _DML_IN_CTE])
def test_hybrid_executor_returns_the_denial_as_an_error_dict(sql: str):
    """``athena_run_query`` returns the standard error shape on a denial.

    ``BoundedSqlExecutor`` short-circuits on ``error`` and surfaces it verbatim,
    which is how the reason reaches the user. ``query_execution_id`` is empty
    because nothing was submitted.
    """
    out = hybrid_execution.athena_run_query(
        sql=sql,
        database_name="db",
        catalog_id="AwsDataCatalog",
        s3_output_location="s3://bkt/",
    )
    assert "Query denied" in out["error"]
    assert out["query_execution_id"] == ""


def test_hybrid_executor_surfaces_the_allowlist_reason():
    """The hybrid site reads the VENDORED context, so bind that copy."""
    hybrid_ctx.bind_profile(profile={"tableAllowlist": ["orders"]}, namespace="ns-1")
    out = hybrid_execution.athena_run_query(
        sql="SELECT x FROM salaries",
        database_name="db",
        catalog_id="AwsDataCatalog",
        s3_output_location="s3://bkt/",
    )
    assert "salaries" in out["error"]


# --------------------------------------------------------------------------
# The REST API publish gate (structural — the closure is not importable)
# --------------------------------------------------------------------------


def _find_function(*, source: str, name: str) -> ast.FunctionDef:
    """Locate a function definition anywhere in a module's AST.

    Walks the whole tree rather than scanning module-level statements, because
    the target is nested inside a feature-flag ``if`` block.

    Args:
        source: The module source text.
        name: The function name to find.

    Returns:
        The matching ``ast.FunctionDef``.

    Raises:
        AssertionError: If no function with that name exists.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — was it renamed or removed?")


def _call_lines(*, func: ast.FunctionDef, callee: str) -> List[int]:
    """Return the line numbers of calls to ``callee`` inside ``func``.

    Matches both a bare name (``enforce_sql(...)``) and an attribute access
    (``athena.start_query_execution(...)``).

    Args:
        func: The function to scan.
        callee: The callee name to match.

    Returns:
        Line numbers, in AST-walk order.
    """
    lines: List[int] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (
            target.id
            if isinstance(target, ast.Name)
            else target.attr
            if isinstance(target, ast.Attribute)
            else ""
        )
        if name == callee:
            lines.append(node.lineno)
    return lines


def test_publish_gate_firewalls_before_it_submits():
    """The rule publish gate calls ``enforce_sql`` before ``start_query_execution``.

    Asserted on the source because the function is a closure behind
    ``if _RULES_ENABLED and _NEPTUNE_GATEWAY_URL:`` and cannot be imported
    without a configured Neptune gateway. Ordering is the point: a firewall call
    placed after submission would validate a statement that already ran.
    """
    source = (_REPO_ROOT / "lambda" / "rest-api" / "main.py").read_text()
    func = _find_function(source=source, name="_execute_athena_sql")
    enforced = _call_lines(func=func, callee="enforce_sql")
    submitted = _call_lines(func=func, callee="start_query_execution")
    assert enforced, "the publish gate no longer firewalls its smoke query"
    assert submitted, "start_query_execution vanished — this test needs updating"
    assert min(enforced) < min(submitted)


# --------------------------------------------------------------------------
# The vendored copies stay behaviourally identical to the canonical ones
# --------------------------------------------------------------------------

# Each vendored module's header promises "fix a security bug in BOTH copies" and
# points here for enforcement. A drift in either direction is a hole: the shared
# copy is what the REST API and metadata agent use, the vendored copy is what
# ships inside the hybrid container image.
_VENDORED = ["firewall_context.py", "grant_profile.py", "sql_firewall.py"]


def _is_import_only(*, node: ast.stmt) -> bool:
    """Report whether a top-level statement contributes nothing but imports.

    Matches a plain import and also the ``try: from agents.shared…  except
    ImportError: from shared…`` block the shared copies use to work under both
    the Lambda and the flat-container layouts. That block has no vendored
    counterpart (the vendored copy imports relatively), so it must not count as
    drift.

    Args:
        node: A module-level statement.

    Returns:
        True when the statement is an import, or a try/except containing only
        imports.
    """
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return True
    if isinstance(node, ast.Try):
        nested = [*node.body, *node.orelse, *node.finalbody]
        for handler in node.handlers:
            nested.extend(handler.body)
        return bool(nested) and all(
            isinstance(inner, (ast.Import, ast.ImportFrom)) for inner in nested
        )
    return False


def _logic_only(*, path: Path) -> str:
    """Normalize a module to its logic, dropping the docstring and imports.

    The two copies legitimately differ in exactly those places — the vendored
    header explains the duplication, and its imports are relative
    (``from .grant_profile import …``) because the hybrid isolation guard bans
    absolute ``agents.*`` imports. Everything else, function docstrings
    included, must match.

    Args:
        path: The module file to normalize.

    Returns:
        Unparsed source with the module docstring and every import-only
        top-level statement removed, so a diff reflects only behavioural drift.
    """
    tree = ast.parse(path.read_text())
    body = [
        node
        for node in tree.body
        if not _is_import_only(node=node)
        and not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


@pytest.mark.parametrize("filename", _VENDORED)
def test_vendored_firewall_module_matches_the_canonical_copy(filename: str):
    """The hybrid package's copy has not drifted from ``agents/shared``."""
    shared = _REPO_ROOT / "agents" / "shared" / filename
    vendored = _REPO_ROOT / "agents" / "semantic_layer_query_agent" / filename
    assert vendored.exists(), f"{filename} is missing from the hybrid package"
    assert _logic_only(path=shared) == _logic_only(path=vendored), (
        f"{filename} has drifted between agents/shared and "
        "agents/semantic_layer_query_agent — a security fix applied to one copy "
        "must be applied to both"
    )


# --------------------------------------------------------------------------
# The namespace and the profile are resolved server-side, never from the payload
# --------------------------------------------------------------------------


def _fake_metadata_table(*, items: List[Dict[str, Any]], monkeypatch) -> MagicMock:
    """Point ``namespace_for_ontology`` at an in-memory metadata table.

    ``namespace_for_ontology`` imports boto3 lazily, so patching the module
    attribute is enough — no AWS call is made.

    Args:
        items: The rows the table's ``query`` should return.
        monkeypatch: pytest's monkeypatch fixture.

    Returns:
        The fake ``Table`` mock, for call assertions.
    """
    import boto3

    table = MagicMock()
    table.query.return_value = {"Items": items}
    resource = MagicMock()
    resource.Table.return_value = table
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: resource)
    monkeypatch.setenv("ONTOLOGY_METADATA_TABLE", "sl-metadata")
    return table


def test_namespace_is_read_from_the_ontology_config_row(monkeypatch):
    """The authorization namespace comes from the table, not the request.

    A caller who could name their own namespace could name one where their
    grants declare no restrictions, and escape the restrictions on the namespace
    they are actually querying.
    """
    _fake_metadata_table(
        items=[{"version": "v1", "namespaceId": "ns-finance"}], monkeypatch=monkeypatch
    )
    assert shared_ctx.namespace_for_ontology(ontology_id="ont-1") == "ns-finance"


def test_namespace_resolution_takes_the_highest_version(monkeypatch):
    """A rebuilt layer writes a new version row; the latest binding wins."""
    _fake_metadata_table(
        items=[
            {"version": "v2", "namespaceId": "ns-new"},
            {"version": "v11", "namespaceId": "ns-newest"},
            {"version": "v1", "namespaceId": "ns-old"},
        ],
        monkeypatch=monkeypatch,
    )
    assert shared_ctx.namespace_for_ontology(ontology_id="ont-1") == "ns-newest"


def test_namespace_resolution_yields_empty_for_an_unknown_ontology(monkeypatch):
    """No config row → no namespace, so no data restrictions are merged."""
    _fake_metadata_table(items=[], monkeypatch=monkeypatch)
    assert shared_ctx.namespace_for_ontology(ontology_id="ont-missing") == ""


def test_namespace_resolution_yields_empty_without_a_metadata_table(monkeypatch):
    """An unconfigured table cannot be read, so nothing is claimed about it."""
    monkeypatch.delenv("ONTOLOGY_METADATA_TABLE", raising=False)
    assert shared_ctx.namespace_for_ontology(ontology_id="ont-1") == ""


def test_bind_from_context_parses_a_stringified_groups_claim(monkeypatch):
    """The HTTP API JWT authorizer stringifies multi-valued claims.

    ``cognito:groups`` arrives as ``"[Admin Users]"``, not a JSON array. Reading
    it as a list would iterate its characters and resolve grants for groups
    named ``[``, ``A``, ``d``, …
    """
    seen: Dict[str, Any] = {}

    def _capture(*, user_id: str, email: str, groups: Optional[List[str]], namespace: str):
        """Record the resolve arguments instead of hitting DynamoDB.

        Args:
            user_id: The subject.
            email: The email claim.
            groups: The parsed group list.
            namespace: The namespace id.

        Returns:
            An identity-only ResolvedProfile.
        """
        seen.update(user_id=user_id, email=email, groups=groups, namespace=namespace)
        from agents.shared.grant_profile import ResolvedProfile

        return ResolvedProfile(user_id=user_id, email=email, groups=list(groups or []))

    monkeypatch.setattr(shared_ctx, "resolve_profile", _capture)

    # A JWT whose payload segment decodes to the claims below. Only the payload
    # is read — AgentCore already verified the signature.
    import base64

    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"email": "a@b.com", "cognito:groups": "[Admin Users]"}
        ).encode()
    ).decode().rstrip("=")
    context = MagicMock(request_headers={"Authorization": f"Bearer h.{payload}.s"})

    shared_ctx.bind_from_context(context=context, user_id="sub-1", namespace="ns-1")
    assert seen["groups"] == ["Admin", "Users"]
    assert seen["email"] == "a@b.com"
    assert seen["user_id"] == "sub-1"


def test_bind_caller_propagates_a_grants_read_failure(monkeypatch):
    """An unreadable grants table must fail the invocation, not downgrade it.

    Swallowing this would run the caller's SQL with no restrictions — a profile
    we could not read is not a profile with no restrictions.
    """

    def _boom(**_kwargs):
        """Simulate a DynamoDB failure.

        Returns:
            Never returns.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("ProvisionedThroughputExceededException")

    monkeypatch.setattr(shared_ctx, "resolve_profile", _boom)
    with pytest.raises(RuntimeError):
        shared_ctx.bind_caller(user_id="sub-1", namespace="ns-1")
    assert shared_ctx.current_profile() is None


def test_clear_caller_drops_the_previous_callers_restrictions():
    """A reused container must not inherit the last invocation's profile."""
    shared_ctx.bind_profile(profile={"tableAllowlist": ["orders"]}, namespace="ns-1")
    shared_ctx.clear_caller()
    assert shared_ctx.current_profile() is None
    assert shared_ctx.current_namespace() == ""
