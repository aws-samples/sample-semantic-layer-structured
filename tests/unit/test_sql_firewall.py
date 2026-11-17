"""Unit tests for the serve-time SQL firewall.

Covers the two postures separately: the structural checks that apply to every
query, and the sparse opt-in table/column restrictions that apply only when a
grant profile declares one.
"""
import pytest

from agents.shared.sql_firewall import (
    FirewallResult,
    SQLFirewall,
    UnsafeSQLError,
    enforce_sql,
)


@pytest.fixture()
def firewall() -> SQLFirewall:
    """A firewall instance (stateless, so construction is cheap)."""
    return SQLFirewall()


# ── Structural checks (always on) ─────────────────────────────────────


@pytest.mark.parametrize(
    'sql',
    [
        'SELECT 1',
        'SELECT a, b FROM t WHERE a > 1',
        'SELECT a FROM t UNION SELECT a FROM u',
        'WITH c AS (SELECT a FROM t) SELECT a FROM c',
        'SELECT count(*) FROM t GROUP BY b HAVING count(*) > 2',
    ],
)
def test_read_only_statements_pass(firewall: SQLFirewall, sql: str) -> None:
    """Plain reads, set operations and CTE-wrapped reads all validate."""
    firewall.validate(sql=sql)


@pytest.mark.parametrize(
    'sql',
    [
        'INSERT INTO t VALUES (1)',
        'DROP TABLE t',
        'CREATE TABLE t (a int)',
        'ALTER TABLE t ADD COLUMN b int',
        'DELETE FROM t',
        'UPDATE t SET a = 1',
        'MSCK REPAIR TABLE t',
        'UNLOAD (SELECT 1) TO \'s3://b/p\'',
        'EXPLAIN SELECT 1',
    ],
)
def test_blocked_statement_types_rejected(firewall: SQLFirewall, sql: str) -> None:
    """The pre-parse regex rejects every blocked leading keyword."""
    with pytest.raises(UnsafeSQLError):
        firewall.validate(sql=sql)


def test_comments_do_not_hide_a_blocked_statement(firewall: SQLFirewall) -> None:
    """Comments are stripped before the statement-type regex runs."""
    with pytest.raises(UnsafeSQLError, match='Statement type not allowed'):
        firewall.validate(sql='/* harmless */ DROP TABLE t')
    with pytest.raises(UnsafeSQLError, match='Statement type not allowed'):
        firewall.validate(sql='-- harmless\nDROP TABLE t')


def test_data_modifying_cte_is_deep_scanned(firewall: SQLFirewall) -> None:
    """A DML CTE leaves a Select at the root — the deep walk must catch it.

    This is the bypass the root-node allowlist alone would miss.
    """
    sql = 'WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d'
    with pytest.raises(UnsafeSQLError):
        firewall.validate(sql=sql, dialect='postgres')


def test_dangerous_function_rejected(firewall: SQLFirewall) -> None:
    """Filesystem/network/sleep functions are denied even inside a SELECT."""
    with pytest.raises(UnsafeSQLError, match='Function not allowed'):
        firewall.validate(sql="SELECT pg_read_file('/etc/passwd')")


def test_unparseable_sql_rejected(firewall: SQLFirewall) -> None:
    """A statement sqlglot cannot parse never reaches Athena."""
    with pytest.raises(UnsafeSQLError):
        firewall.validate(sql='SELECT FROM WHERE ((((')


# ── Sparse opt-in: no restrictions declared ───────────────────────────


def test_unrestricted_profile_allows(firewall: SQLFirewall) -> None:
    """No declared restriction means the structural checks are the whole gate."""
    result = firewall.evaluate(sql='SELECT * FROM anything', profile={})
    assert isinstance(result, FirewallResult)
    assert result.denied is False
    assert result.authorized_sql == 'SELECT * FROM anything'


def test_none_profile_allows(firewall: SQLFirewall) -> None:
    """A missing profile behaves like an unrestricted one."""
    assert firewall.evaluate(sql='SELECT a FROM t', profile=None).denied is False


# ── Table allowlist ───────────────────────────────────────────────────


def test_table_allowlist_permits_listed_table(firewall: SQLFirewall) -> None:
    """A table named in the allowlist is permitted."""
    profile = {'tableAllowlist': ['allowed_table']}
    assert (
        firewall.evaluate(sql='SELECT a FROM allowed_table', profile=profile).denied
        is False
    )


def test_table_allowlist_denies_unlisted_table(firewall: SQLFirewall) -> None:
    """A table outside the allowlist is denied and named in the reason."""
    profile = {'tableAllowlist': ['allowed_table']}
    result = firewall.evaluate(sql='SELECT a FROM other_table', profile=profile)
    assert result.denied is True
    assert 'other_table' in (result.reason or '')
    assert result.authorized_sql == ''


def test_table_allowlist_is_case_insensitive(firewall: SQLFirewall) -> None:
    """Varying identifier case must not bypass the allowlist."""
    profile = {'tableAllowlist': ['orders']}
    assert firewall.evaluate(sql='SELECT a FROM ORDERS', profile=profile).denied is False
    assert (
        firewall.evaluate(sql='SELECT a FROM Payments', profile=profile).denied is True
    )


def test_empty_allowlist_denies_everything(firewall: SQLFirewall) -> None:
    """An explicitly empty allowlist is a deny-all, not an absent restriction.

    Regression guard for the fail-open bug: treating ``[]`` as falsy dropped the
    restriction and allowed every table.
    """
    result = firewall.evaluate(sql='SELECT a FROM t', profile={'tableAllowlist': []})
    assert result.denied is True


def test_allowlist_covers_joined_tables(firewall: SQLFirewall) -> None:
    """Every table in a join must be permitted, not just the first."""
    profile = {'tableAllowlist': ['a']}
    result = firewall.evaluate(
        sql='SELECT a.x FROM a JOIN b ON a.id = b.id', profile=profile
    )
    assert result.denied is True
    assert 'b' in (result.reason or '')


def test_cte_name_is_not_treated_as_a_source_table(firewall: SQLFirewall) -> None:
    """A CTE alias must not have to appear in the allowlist."""
    profile = {'tableAllowlist': ['t']}
    sql = 'WITH c AS (SELECT a FROM t) SELECT a FROM c'
    assert firewall.evaluate(sql=sql, profile=profile).denied is False


def test_malformed_allowlist_denies(firewall: SQLFirewall) -> None:
    """A non-list allowlist denies rather than iterating a string's characters."""
    result = firewall.evaluate(sql='SELECT a FROM t', profile={'tableAllowlist': 't'})
    assert result.denied is True
    assert 'Invalid tableAllowlist' in (result.reason or '')


# ── Column denylist ───────────────────────────────────────────────────


def test_column_denylist_denies_referenced_column(firewall: SQLFirewall) -> None:
    """A denied column referenced by name is blocked."""
    profile = {'columnDenylist': {'parties': ['ssn']}}
    result = firewall.evaluate(sql='SELECT ssn FROM parties', profile=profile)
    assert result.denied is True
    assert 'parties.ssn' in (result.reason or '')


def test_column_denylist_allows_other_columns(firewall: SQLFirewall) -> None:
    """Columns outside the denylist are unaffected."""
    profile = {'columnDenylist': {'parties': ['ssn']}}
    assert (
        firewall.evaluate(sql='SELECT name FROM parties', profile=profile).denied
        is False
    )


def test_select_star_over_restricted_table_denied(firewall: SQLFirewall) -> None:
    """``SELECT *`` cannot prove the denied columns are excluded, so it fails closed."""
    profile = {'columnDenylist': {'parties': ['ssn']}}
    result = firewall.evaluate(sql='SELECT * FROM parties', profile=profile)
    assert result.denied is True
    assert 'SELECT *' in (result.reason or '')


def test_qualified_star_over_restricted_table_denied(firewall: SQLFirewall) -> None:
    """``t.*`` is the same problem as a bare star."""
    profile = {'columnDenylist': {'parties': ['ssn']}}
    result = firewall.evaluate(
        sql='SELECT p.* FROM parties p JOIN c ON p.id = c.id', profile=profile
    )
    assert result.denied is True


def test_alias_resolves_to_the_restricted_table(firewall: SQLFirewall) -> None:
    """An alias must not hide a denied column."""
    profile = {'columnDenylist': {'parties': ['ssn']}}
    result = firewall.evaluate(sql='SELECT p.ssn FROM parties p', profile=profile)
    assert result.denied is True


def test_unqualified_column_treated_as_any_in_scope_table(
    firewall: SQLFirewall,
) -> None:
    """An unqualified denied column is blocked — it may belong to the restricted table."""
    profile = {'columnDenylist': {'parties': ['ssn']}}
    result = firewall.evaluate(
        sql='SELECT ssn FROM parties JOIN c ON parties.id = c.id', profile=profile
    )
    assert result.denied is True


def test_denylist_for_untouched_table_is_ignored(firewall: SQLFirewall) -> None:
    """A denylist on a table the query never references imposes nothing."""
    profile = {'columnDenylist': {'parties': ['ssn']}}
    assert (
        firewall.evaluate(sql='SELECT ssn FROM other', profile=profile).denied is False
    )


def test_malformed_denylist_denies(firewall: SQLFirewall) -> None:
    """A non-mapping denylist denies."""
    result = firewall.evaluate(sql='SELECT a FROM t', profile={'columnDenylist': []})
    assert result.denied is True


def test_denylist_with_non_list_columns_denies(firewall: SQLFirewall) -> None:
    """Per-table denylist values must be sequences."""
    result = firewall.evaluate(
        sql='SELECT a FROM t', profile={'columnDenylist': {'t': 'a'}}
    )
    assert result.denied is True


# ── enforce() / enforce_sql() ─────────────────────────────────────────


def test_enforce_returns_sql_when_allowed(firewall: SQLFirewall) -> None:
    """The raise-on-deny form returns the cleared SQL."""
    assert firewall.enforce(sql='SELECT 1', profile={}) == 'SELECT 1'


def test_enforce_raises_on_denial(firewall: SQLFirewall) -> None:
    """An authorization miss raises rather than returning a result to ignore."""
    with pytest.raises(UnsafeSQLError, match='Access denied'):
        firewall.enforce(sql='SELECT a FROM other', profile={'tableAllowlist': ['t']})


def test_module_level_enforce_sql_wrapper() -> None:
    """The convenience wrapper shares the singleton's behaviour."""
    assert enforce_sql(sql='SELECT 1') == 'SELECT 1'
    with pytest.raises(UnsafeSQLError):
        enforce_sql(sql='DROP TABLE t')


# ── extract_tables ────────────────────────────────────────────────────


def test_extract_tables_returns_qualified_names(firewall: SQLFirewall) -> None:
    """Audit logging gets catalog/db-qualified names."""
    tables = SQLFirewall.extract_tables(sql='SELECT a FROM cat.db.t JOIN db.u ON 1=1')
    assert 'cat.db.t' in tables
    assert 'db.u' in tables


def test_extract_tables_never_raises(firewall: SQLFirewall) -> None:
    """A parse failure yields an empty list — it must never be a basis for an allow."""
    assert SQLFirewall.extract_tables(sql='SELECT FROM ((((') == []
