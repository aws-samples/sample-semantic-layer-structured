"""Serve-time SQL firewall — the last gate before ``start_query_execution``.

Ported from the Context Ontology Accelerator's
``coa_serve/tier2/sql_firewall.py`` so the two projects enforce the same
posture. Two layers with deliberately different failure modes:

**Always-on structural checks** (every query, every profile)
  - a pre-parse regex on comment-stripped SQL rejects blocked statement
    types before a dialect is even known;
  - SQL sqlglot cannot parse is denied outright — an unparseable statement
    cannot be *proven* read-only, and the regex above only inspects the
    leading keyword;
  - only ``SELECT`` / ``UNION`` / ``INTERSECT`` / ``EXCEPT`` may be the root;
  - a deep AST walk rejects data-modifying nodes *anywhere* in the tree, so
    ``WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d`` is blocked even
    though its root is a ``Select``;
  - a denylist of filesystem / network / sleep functions.

**Sparse opt-in table & column restrictions**
  A grant profile that declares neither ``tableAllowlist`` nor
  ``columnDenylist`` imposes no data-level constraint and the query is
  allowed — action- and namespace-level authorization is enforced upstream by
  ``authz.middleware.AuthzMiddleware``. Inside a *restrictive* profile the
  firewall additionally fails closed on anything it cannot fully analyze for
  table/column references, and ``SELECT *`` over a column-restricted table is
  denied because the denied columns cannot be proven excluded.

Unlike COA this module carries no Cedar sub-gate: SL invokes its agent
runtimes only through the REST API, which has already evaluated Cedar for the
request's action and namespace. See
``docs/plans/2026-08-23-namespaces-authz-firewall-design.md`` §3.3.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple, Type

import sqlglot

logger = logging.getLogger(__name__)

# SQL comments are stripped before the statement-type regex runs, otherwise
# `/* hi */ DROP TABLE t` would slip past a `^\s*DROP` match.
_COMMENT_PATTERN = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)

_BLOCKED_STATEMENTS = re.compile(
    r"^\s*(MSCK\s+REPAIR|CREATE|DROP|ALTER|INSERT|DELETE|UPDATE|MERGE"
    r"|UNLOAD|PREPARE|EXECUTE|EXPLAIN)\b",
    re.IGNORECASE,
)

_DANGEROUS_FUNCTIONS = frozenset(
    {
        'pg_read_file',
        'pg_read_binary_file',
        'pg_ls_dir',
        'pg_stat_file',
        'dblink',
        'dblink_exec',
        'dblink_connect',
        'lo_import',
        'lo_export',
        'copy',
        'pg_sleep',
    }
)

# Allowlist: only these root AST node types pass validation. An allowlist is
# safer than a denylist — a statement type sqlglot learns to parse tomorrow is
# rejected by default rather than silently permitted.
_SAFE_STATEMENT_TYPES: Tuple[Type[sqlglot.exp.Expression], ...] = (
    sqlglot.exp.Select,
    sqlglot.exp.Union,
    sqlglot.exp.Intersect,
    sqlglot.exp.Except,
)

# Deep-scan denylist: nodes that must not appear anywhere in the AST, even
# nested inside an otherwise-safe set operation or CTE.
_DATA_MODIFYING_NODES: Tuple[Type[sqlglot.exp.Expression], ...] = (
    sqlglot.exp.Insert,
    sqlglot.exp.Update,
    sqlglot.exp.Delete,
    sqlglot.exp.Merge,
    sqlglot.exp.Into,
    sqlglot.exp.Create,
    sqlglot.exp.Drop,
    sqlglot.exp.Alter,
)

# Grant-profile keys carrying the principal's resolved data-access
# restrictions. Written by the grants API, merged deny-wins by
# ``agents.shared.grant_profile``.
_TABLE_ALLOWLIST_KEY = 'tableAllowlist'
_COLUMN_DENYLIST_KEY = 'columnDenylist'

# SL executes everything through Athena (Trino-derived), so parse with the
# Athena dialect rather than COA's generic `trino`.
_DEFAULT_DIALECT = 'athena'


class UnsafeSQLError(ValueError):
    """Raised when SQL fails structural safety validation or is denied."""


@dataclass(frozen=True)
class _QueryRefs:
    """Table and column references extracted from one SQL statement.

    Table names are bare (last identifier component) so they match the grant
    profile's allowlist/denylist keys. Columns are grouped by resolved table;
    unqualified columns are tracked separately because they may belong to any
    in-scope table and are therefore evaluated fail-closed.
    """

    tables: frozenset
    columns_by_table: Dict[str, Set[str]]
    unqualified_columns: frozenset
    star_tables: frozenset
    bare_star: bool


@dataclass(frozen=True)
class FirewallResult:
    """Outcome of a firewall evaluation.

    Attributes:
        denied: True when the query was blocked.
        authorized_sql: The SQL cleared for execution (empty when denied).
        reason: Human-readable denial reason, or None when allowed.
    """

    denied: bool
    authorized_sql: str
    reason: Optional[str] = None


class SQLFirewall:
    """Structural safety plus table/column authorization for SQL execution.

    Structural checks always run. Table/column enforcement is sparse opt-in:
    it applies only when the caller's grant profile declares a restriction.
    """

    def check_statement_type(self, *, sql: str) -> None:
        """Reject blocked statement types without parsing.

        Dialect-independent, so it can gate before the engine/dialect is
        known. ``validate`` calls this first; it is public so callers that
        must fail fast before resolving a dialect can use it alone.

        Args:
            sql: The raw SQL statement.

        Returns:
            None.

        Raises:
            UnsafeSQLError: If the leading keyword is a blocked statement type.
        """
        stripped = _COMMENT_PATTERN.sub(' ', sql).strip()
        if _BLOCKED_STATEMENTS.match(stripped):
            raise UnsafeSQLError('Statement type not allowed')

    def validate(self, *, sql: str, dialect: str = _DEFAULT_DIALECT) -> None:
        """Validate that ``sql`` is a read-only statement with no unsafe calls.

        Args:
            sql: The SQL statement to validate.
            dialect: sqlglot read-dialect. Callers validating SQL already
                transpiled to another engine's dialect must pass that dialect,
                or a valid statement is rejected as unparseable.

        Returns:
            None.

        Raises:
            UnsafeSQLError: If the statement is not a safe read-only query.
        """
        self.check_statement_type(sql=sql)

        try:
            parsed = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.ParseError as exc:
            raise UnsafeSQLError('Failed to parse SQL statement') from exc

        if not isinstance(parsed, _SAFE_STATEMENT_TYPES):
            raise UnsafeSQLError(
                f'Only SELECT statements are allowed, got: {type(parsed).__name__}'
            )

        # Deep scan — a data-modifying CTE, SELECT INTO, or DML in a subquery
        # all leave a safe-looking root node.
        for node in parsed.walk():
            if isinstance(node, _DATA_MODIFYING_NODES):
                raise UnsafeSQLError(
                    f"Data-modifying operation '{type(node).__name__}' "
                    'found inside statement'
                )

        for func in parsed.find_all(sqlglot.exp.Anonymous, sqlglot.exp.Func):
            func_name = str(getattr(func, 'name', '') or '').lower()
            if func_name in _DANGEROUS_FUNCTIONS:
                raise UnsafeSQLError(f'Function not allowed: {func_name}')

    def evaluate(
        self,
        *,
        sql: str,
        profile: Optional[Dict[str, Any]] = None,
        namespace: str = '',
        dialect: str = _DEFAULT_DIALECT,
    ) -> FirewallResult:
        """Validate safety, then enforce the profile's table/column restrictions.

        Comparisons are case-insensitive: unquoted SQL identifiers are
        case-insensitive across Athena/Postgres/MySQL, so both the profile and
        the extracted references are lower-cased before any set operation —
        otherwise ``SELECT * FROM ORDERS`` would bypass an ``orders`` denylist.

        Args:
            sql: The SQL statement to authorize.
            profile: The caller's resolved grant profile. Recognized keys:
                ``tableAllowlist`` (list[str]) and ``columnDenylist``
                (dict[str, list[str]]).
            namespace: Namespace the query runs in. Recorded on the audit log
                line; action-level authorization happened upstream.
            dialect: sqlglot read-dialect used for parsing.

        Returns:
            A FirewallResult. Note that an authorization miss *returns* a
            denial rather than raising — use :meth:`enforce` to raise.

        Raises:
            UnsafeSQLError: If the statement fails structural validation.
        """
        self.validate(sql=sql, dialect=dialect)

        profile = profile or {}
        allowlist_raw = profile.get(_TABLE_ALLOWLIST_KEY)
        denylist_raw = profile.get(_COLUMN_DENYLIST_KEY)

        # Fail-closed type validation — a malformed grant profile must not
        # silently iterate the characters of a string or raise AttributeError
        # somewhere further down.
        if allowlist_raw is not None and not isinstance(allowlist_raw, (list, tuple)):
            return self._deny(reason='Invalid tableAllowlist format: expected list')
        if denylist_raw is not None and not isinstance(denylist_raw, dict):
            return self._deny(reason='Invalid columnDenylist format: expected mapping')

        # ``is not None``, not truthiness: an ABSENT allowlist means "no table
        # constraint", an explicitly EMPTY one means "no tables permitted".
        # Collapsing the two makes a deny-all grant fail OPEN, because the
        # empty list is falsy and the unrestricted branch below allows
        # everything. This exact bug is on record in COA.
        allowlist: Optional[Set[str]] = None
        if allowlist_raw is not None:
            try:
                allowlist = {str(t).lower() for t in allowlist_raw}
            except TypeError:
                return self._deny(
                    reason='Invalid tableAllowlist format: non-iterable members'
                )

        denylist: Dict[str, Set[str]] = {}
        if denylist_raw:
            for table, cols in denylist_raw.items():
                if not isinstance(cols, (list, tuple, set, frozenset)):
                    return self._deny(
                        reason=(
                            f"Invalid columnDenylist format for table '{table}': "
                            'expected list'
                        )
                    )
                denylist[str(table).lower()] = {str(c).lower() for c in cols}

        # Sparse opt-in: no data-level restriction on this grant, so the
        # structural checks above are the whole gate.
        if allowlist is None and not denylist:
            logger.info(
                'sql_firewall_evaluate decision=allow restricted=false namespace=%s',
                namespace,
            )
            return FirewallResult(denied=False, authorized_sql=sql)

        # Fail-closed inside a restrictive profile: anything we cannot fully
        # analyze for table/column references is denied.
        refs = self._analyze_refs(sql=sql, dialect=dialect)
        if refs is None:
            return self._deny(reason='Query could not be parsed for authorization')

        if allowlist is not None:
            unauthorized = set(refs.tables) - allowlist
            if unauthorized:
                return self._deny(
                    reason=f"Access denied to table(s): {', '.join(sorted(unauthorized))}"
                )

        for table_lc, denied_columns in denylist.items():
            if table_lc not in refs.tables:
                continue
            reason = self._evaluate_columns(
                table=table_lc, denied=denied_columns, refs=refs
            )
            if reason:
                return self._deny(reason=reason)

        logger.info(
            'sql_firewall_evaluate decision=allow restricted=true namespace=%s tables=%s',
            namespace,
            sorted(refs.tables),
        )
        return FirewallResult(denied=False, authorized_sql=sql)

    def enforce(
        self,
        *,
        sql: str,
        profile: Optional[Dict[str, Any]] = None,
        namespace: str = '',
        dialect: str = _DEFAULT_DIALECT,
    ) -> str:
        """Evaluate ``sql`` and return it, raising on any denial.

        The raise-on-deny form used at execution sites, so a caller that
        forgets to inspect ``FirewallResult.denied`` cannot accidentally run a
        denied statement.

        Args:
            sql: The SQL statement to authorize.
            profile: The caller's resolved grant profile.
            namespace: Namespace the query runs in.
            dialect: sqlglot read-dialect used for parsing.

        Returns:
            The SQL cleared for execution.

        Raises:
            UnsafeSQLError: If the statement fails structural validation or is
                denied by the profile's restrictions.
        """
        result = self.evaluate(
            sql=sql, profile=profile, namespace=namespace, dialect=dialect
        )
        if result.denied:
            raise UnsafeSQLError(result.reason or 'Access denied by SQL firewall')
        return result.authorized_sql

    @staticmethod
    def _deny(*, reason: str) -> FirewallResult:
        """Build a denial result and log it at WARN level.

        Args:
            reason: The denial reason surfaced to the caller.

        Returns:
            A denied FirewallResult carrying ``reason``.
        """
        logger.warning('sql_firewall_evaluate decision=deny reason=%s', reason)
        return FirewallResult(denied=True, authorized_sql='', reason=reason)

    @staticmethod
    def _evaluate_columns(
        *, table: str, denied: Set[str], refs: _QueryRefs
    ) -> Optional[str]:
        """Return a denial reason if the query exposes denied columns of ``table``.

        Fails closed on ``SELECT *`` and ``table.*`` (the denied columns cannot
        be proven excluded) and treats unqualified columns as potentially
        belonging to any in-scope table.

        Args:
            table: Lower-cased bare table name being evaluated.
            denied: Lower-cased column names denied on that table.
            refs: References extracted from the statement.

        Returns:
            A denial reason, or None when no denied column is exposed.
        """
        if not denied:
            return None
        if refs.bare_star or table in refs.star_tables:
            return (
                'Access denied: SELECT * may expose restricted columns of '
                f"'{table}'"
            )
        blocked = (
            refs.columns_by_table.get(table, set()) | set(refs.unqualified_columns)
        ) & denied
        if blocked:
            cols = ', '.join(f'{table}.{c}' for c in sorted(blocked))
            return f'Access denied to column(s): {cols}'
        return None

    @staticmethod
    def _analyze_refs(*, sql: str, dialect: str) -> Optional[_QueryRefs]:
        """Extract the table/column references ``sql`` touches.

        CTE names are excluded so a CTE is never mistaken for a source table.
        Every identifier is lower-cased so comparison with the (also
        lower-cased) grant profile is case-insensitive.

        Args:
            sql: The SQL statement to analyze.
            dialect: sqlglot read-dialect used for parsing.

        Returns:
            The extracted references, or None when the statement cannot be
            parsed (the caller must then fail closed).
        """
        try:
            parsed = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.ParseError:
            logger.warning('sql_firewall_parse_error sql_preview=%s', sql[:200])
            return None
        if parsed is None:
            return None

        cte_names = {
            cte.alias_or_name.lower() for cte in parsed.find_all(sqlglot.exp.CTE)
        }

        # alias or table name (lower-cased) -> bare table name (lower-cased)
        alias_map: Dict[str, str] = {}
        tables: Set[str] = set()
        for tbl in parsed.find_all(sqlglot.exp.Table):
            name = tbl.name
            if not name or name.lower() in cte_names:
                continue
            name_lc = name.lower()
            tables.add(name_lc)
            alias_map[name_lc] = name_lc
            if tbl.alias:
                alias_map[tbl.alias.lower()] = name_lc

        def resolve(qualifier: str) -> str:
            """Map a column qualifier (alias or table name) to a bare table name."""
            return alias_map.get(qualifier.lower(), qualifier.lower())

        star_tables: Set[str] = set()
        bare_star = False
        for star in parsed.find_all(sqlglot.exp.Star):
            parent = star.parent
            if isinstance(parent, sqlglot.exp.Column) and parent.table:
                star_tables.add(resolve(parent.table))
            elif isinstance(parent, sqlglot.exp.Select):
                # Bare `SELECT *` exposes every column of every in-scope table.
                bare_star = True

        columns_by_table: Dict[str, Set[str]] = {}
        unqualified: Set[str] = set()
        for col in parsed.find_all(sqlglot.exp.Column):
            if isinstance(col.this, sqlglot.exp.Star):
                continue  # handled by the star pass above
            name = col.name
            if not name:
                continue
            name_lc = name.lower()
            qualifier = col.table
            if qualifier and qualifier.lower() not in cte_names:
                columns_by_table.setdefault(resolve(qualifier), set()).add(name_lc)
            elif not qualifier:
                unqualified.add(name_lc)

        return _QueryRefs(
            tables=frozenset(tables),
            columns_by_table=columns_by_table,
            unqualified_columns=frozenset(unqualified),
            star_tables=frozenset(star_tables),
            bare_star=bare_star,
        )

    @staticmethod
    def extract_tables(*, sql: str, dialect: str = _DEFAULT_DIALECT) -> List[str]:
        """Return the qualified table names ``sql`` references.

        Used for audit logging, not for authorization decisions — it returns an
        empty list on a parse failure rather than raising, so it must never be
        the basis of an allow.

        Args:
            sql: The SQL statement to inspect.
            dialect: sqlglot read-dialect used for parsing.

        Returns:
            Deduplicated ``catalog.db.table`` names in first-seen order.
        """
        try:
            parsed = sqlglot.parse_one(sql, read=dialect)
        except sqlglot.errors.ParseError:
            logger.warning('sql_firewall_parse_error sql_preview=%s', sql[:200])
            return []
        if parsed is None:
            return []

        tables: List[str] = []
        seen: Set[str] = set()
        for table_node in parsed.find_all(sqlglot.exp.Table):
            parts: List[str] = []
            if table_node.catalog:
                parts.append(table_node.catalog)
            if table_node.db:
                parts.append(table_node.db)
            if table_node.name:
                parts.append(table_node.name)
            if not parts:
                continue
            qualified = '.'.join(parts)
            if qualified not in seen:
                seen.add(qualified)
                tables.append(qualified)

        return tables


# Module-level singleton — the firewall is stateless, so execution sites share
# one instance rather than constructing a new object per query.
_FIREWALL = SQLFirewall()


def enforce_sql(
    *,
    sql: str,
    profile: Optional[Dict[str, Any]] = None,
    namespace: str = '',
    dialect: str = _DEFAULT_DIALECT,
) -> str:
    """Module-level convenience wrapper around :meth:`SQLFirewall.enforce`.

    Args:
        sql: The SQL statement to authorize.
        profile: The caller's resolved grant profile.
        namespace: Namespace the query runs in.
        dialect: sqlglot read-dialect used for parsing.

    Returns:
        The SQL cleared for execution.

    Raises:
        UnsafeSQLError: If the statement fails validation or is denied.
    """
    return _FIREWALL.enforce(
        sql=sql, profile=profile, namespace=namespace, dialect=dialect
    )
