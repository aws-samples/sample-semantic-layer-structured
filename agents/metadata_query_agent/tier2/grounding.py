"""Phase 5 grounding gate — assert generated SQL is fully grounded in the slice.

A deterministic check (no LLM) that every table and column referenced by the
generated SQL appears in the Phase 3 schema slice. This is the structural
enforcement of the eval-analyzer's "pre-SQL schema verification" finding: the
old Tier 3 worker relied on a prompt instruction not to hallucinate columns;
here it is a set-membership check that cannot be reasoned around.

Identifiers are extracted by parsing the SQL with ``sqlglot`` and walking the
AST for ``exp.Table`` and ``exp.Column`` nodes. The check is intentionally
lenient on case and on ``db.table`` vs bare ``table`` matching to avoid false
rejects; string/number literal constants the user supplied are never flagged
(only column *references* must resolve to a slice column).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Set, Tuple

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)


def _alias_to_table(tree: "exp.Expression") -> Dict[str, str]:
    """Map each table alias (and bare table name) to its real table name.

    e.g. ``FROM normalized.phone ph`` →  ``{"ph": "phone", "phone": "phone"}``.
    Lets the grounding check resolve a qualified column like ``ph.is_primary``
    back to the ``phone`` table so it is checked against THAT table's columns,
    not any table in the slice (the cross-table false-negative fix).
    """
    mapping: Dict[str, str] = {}
    for tbl in tree.find_all(exp.Table):
        if not tbl.name:
            continue
        real = tbl.name.lower()
        mapping[real] = real
        alias = tbl.alias_or_name
        if alias:
            mapping[alias.lower()] = real
    return mapping


def extract_sql_identifiers(sql: str, *, dialect: str) -> Dict[str, Any]:
    """Parse ``sql`` and return the table + (qualified) column identifiers.

    Args:
        sql: SQL text (already syntax-validated by Phase 4).
        dialect: sqlglot dialect identifier (e.g. ``"athena"``).

    Returns:
        ``{"tables": {lower table names...},
           "columns": {lower bare column names...},
           "qualified": [(real_table_or_None, column), ...]}``.
        ``qualified`` resolves each column's table alias to its real table when
        the column was written ``alias.col`` / ``table.col``; an unqualified
        column yields ``(None, col)``.

    SELECT-output aliases (``COUNT(*) AS party_count``) are NOT schema columns,
    so they are excluded from ``columns`` / ``qualified``: an alias re-referenced
    in ``ORDER BY``/``HAVING``/``GROUP BY`` parses as an ``exp.Column`` but
    resolves to the computed output, not a slice column. Without this, an
    aggregate alias like ``party_count`` was falsely flagged ungrounded.
    """
    tree = sqlglot.parse_one(sql, read=dialect)
    alias_map = _alias_to_table(tree)
    # Output aliases declared anywhere in the query (``<expr> AS name``). A bare
    # column re-reference matching one of these is the alias, not a real column,
    # so it must not be grounding-checked against the slice.
    output_aliases: Set[str] = set()
    for node in tree.find_all(exp.Alias):
        if node.alias:
            output_aliases.add(node.alias.lower())
    tables: Set[str] = set()
    columns: Set[str] = set()
    qualified: List[Tuple[Any, str]] = []
    for node in tree.walk():
        if isinstance(node, exp.Table):
            if node.name:
                tables.add(node.name.lower())
        elif isinstance(node, exp.Column):
            if not node.name:
                continue
            col = node.name.lower()
            tbl_qualifier = node.table  # alias or table prefix, '' if unqualified
            # An UNqualified reference whose name matches a SELECT output alias is
            # that alias (e.g. ``ORDER BY party_count``), not a schema column —
            # skip it. A qualified ref (``t.party_count``) is a real column ref,
            # so it is still checked.
            if not tbl_qualifier and col in output_aliases:
                continue
            columns.add(col)
            if tbl_qualifier:
                real = alias_map.get(tbl_qualifier.lower())
                qualified.append((real, col))
            else:
                qualified.append((None, col))
    return {"tables": tables, "columns": columns, "qualified": qualified}


def _parse_slice(slice_text: str) -> Tuple[Set[str], Set[str], Dict[str, Set[str]]]:
    """Parse the slice JSON into grounding lookups.

    Returns ``(slice_tables, all_columns, columns_by_table)`` lowercased:
      * ``slice_tables`` — every table id, both ``db.table`` and bare ``table``.
      * ``all_columns`` — union of all column names across the slice (used for
        unqualified column references, which can't be pinned to one table).
      * ``columns_by_table`` — ``{bare_table: {column names}}`` so a qualified
        column (``ph.is_primary`` → table ``phone``) is checked against the
        columns of THAT table only. This is what prevents a column owned by a
        different slice table from falsely grounding a qualified reference.
    """
    slice_tables: Set[str] = set()
    all_columns: Set[str] = set()
    by_table: Dict[str, Set[str]] = {}
    try:
        obj = json.loads(slice_text) if slice_text else {}
    except (json.JSONDecodeError, TypeError):
        return slice_tables, all_columns, by_table
    for tid in obj.get("tables", []) or []:
        tl = str(tid).lower()
        slice_tables.add(tl)
        if "." in tl:
            slice_tables.add(tl.split(".", 1)[1])  # bare table name too
    for col in obj.get("columns", []) or []:
        if not isinstance(col, dict):
            continue
        name = (col.get("name") or "").lower()
        if not name:
            continue
        all_columns.add(name)
        tid = str(col.get("table_id") or "").lower()
        bare = tid.split(".", 1)[1] if "." in tid else tid
        if bare:
            by_table.setdefault(bare, set()).add(name)
    # JOIN keys are columns too — fold them into both lookups so a join
    # predicate column dropped by the budget fitter still grounds. Without a
    # reliable owning table, add them to every slice table's set.
    for join in obj.get("joins", []) or []:
        if not isinstance(join, dict):
            continue
        for side, key in (("from", "from_col"), ("to", "to_col")):
            v = (join.get(key) or "").lower()
            if not v:
                continue
            all_columns.add(v)
            owner = str(join.get(side) or "").lower()
            bare = owner.split(".", 1)[1] if "." in owner else owner
            if bare:
                by_table.setdefault(bare, set()).add(v)
    return slice_tables, all_columns, by_table


def check_grounding(*, sql: str, slice_text: str, dialect: str) -> List[str]:
    """Return the identifiers in ``sql`` that are absent from the slice.

    An empty list means the SQL is fully grounded. A non-empty list contains
    human-readable identifiers (``"table:foo"`` / ``"column:bar"`` /
    ``"column:phone.is_primary"``) the Phase 5 node feeds back into regeneration.

    Grounding is **table-qualified**: a column written ``ph.is_primary`` is
    checked against the columns of the table ``ph`` aliases (``phone``) — NOT
    against the union of all slice columns. Otherwise a column owned by a
    different slice table (e.g. ``relation.is_primary``) would falsely ground a
    hallucinated ``phone.is_primary`` reference.

    Args:
        sql: The generated SQL.
        slice_text: The serialized Phase 3 slice JSON.
        dialect: sqlglot dialect identifier.
    """
    try:
        ids = extract_sql_identifiers(sql, dialect=dialect)
    except sqlglot.errors.ParseError as exc:
        # Phase 4 already validated syntax; a parse failure here is unexpected.
        # Treat as ungrounded-but-unactionable so the caller degrades rather
        # than looping (no identifiers to add).
        logger.warning("grounding parse failed (unexpected post-Phase4): %s", exc)
        return []

    slice_tables, all_columns, by_table = _parse_slice(slice_text)
    missing: List[str] = []

    for table in sorted(ids["tables"]):
        if table in slice_tables:
            continue
        if any(st == table or st.endswith(f".{table}") for st in slice_tables):
            continue
        missing.append(f"table:{table}")

    # De-dupe missing column reports while preserving the qualified table when
    # we have it (more actionable for the regeneration feedback).
    seen: Set[str] = set()
    for real_table, column in ids["qualified"]:
        if column in ("*", ""):
            continue
        if real_table is not None and real_table in by_table:
            # Qualified to a known slice table → must be in THAT table's columns.
            grounded = column in by_table[real_table]
            label = f"column:{real_table}.{column}"
        elif real_table is not None and any(
                st == real_table or st.endswith(f".{real_table}")
                for st in slice_tables):
            # Qualified to a table that IS in the slice but contributed NO parsed
            # columns (the budget fitter dropped them, or the table carried only
            # audit cols not surfaced in the slice). Fail closed rather than
            # silently skip: the column is grounded only if it appears somewhere
            # in the slice's columns; otherwise it is hallucinated. This closes
            # the blind spot where a column qualified to a column-less slice table
            # (e.g. holding_payout.payout_frequency, where holding_payout was
            # listed in `tables` but had zero `columns` entries) slipped through
            # the gate and reached Athena. The all_columns fallback keeps this no
            # stricter than the unqualified branch below, so a real column that
            # merely survived on another slice table is not falsely flagged.
            grounded = column in all_columns
            label = f"column:{real_table}.{column}"
        elif real_table is not None:
            # Qualified to a table genuinely absent from the slice — the table
            # itself is already reported missing above; skip the column to avoid
            # duplicate noise.
            continue
        else:
            # Unqualified column — can't pin to one table, so accept if present
            # anywhere in the slice (best-effort; ambiguous by construction).
            grounded = column in all_columns
            label = f"column:{column}"
        if not grounded and label not in seen:
            seen.add(label)
            missing.append(label)

    return missing


def build_grounding_feedback(*, missing: List[str], slice_text: str) -> str:
    """Turn a raw ``missing`` list into actionable regeneration feedback.

    The bare ``check_grounding`` output (e.g. ``["column:holding_payout.payout_frequency"]``)
    tells the model WHAT was wrong but not what to use instead — so the next
    round often re-hallucinates a sibling guess. This enriches each entry with
    the columns the flagged table ACTUALLY has in the slice, and lists the other
    slice tables as candidates, so a degrade can become a corrected answer
    in-loop rather than burning a regeneration round.

    Args:
        missing: identifiers from :func:`check_grounding`
            (``"table:foo"`` / ``"column:bar"`` / ``"column:tbl.col"``).
        slice_text: the serialized Phase 3 slice JSON.

    Returns:
        A multi-line feedback string naming, per flagged column, the real
        columns available on its table; per flagged table, the slice tables it
        could have meant; and a closing reminder of all slice tables. Falls back
        to a plain comma-join when the slice can't be parsed.
    """
    slice_tables, _all_columns, by_table = _parse_slice(slice_text)
    if not missing:
        return ""
    # Bare table name (no db. prefix) for display, preserving qualified form.
    all_table_names = sorted({
        st.split(".", 1)[1] if "." in st else st for st in slice_tables
    })

    def _cols_for(table: str) -> str:
        """Comma-list the slice columns for ``table`` (bare name), or a marker."""
        cols = sorted(by_table.get(table, set()))
        return ", ".join(cols) if cols else "(no columns in slice for this table)"

    lines: List[str] = []
    for item in missing:
        if item.startswith("column:"):
            ref = item[len("column:"):]
            if "." in ref:
                tbl, col = ref.split(".", 1)
                lines.append(
                    f"- column `{col}` does not exist on `{tbl}`. Columns "
                    f"available on `{tbl}` in the slice: {_cols_for(tbl)}."
                )
            else:
                lines.append(
                    f"- column `{ref}` is not present in any slice table."
                )
        elif item.startswith("table:"):
            tbl = item[len("table:"):]
            lines.append(
                f"- table `{tbl}` is not in the slice. Tables available: "
                f"{', '.join(all_table_names)}."
            )
        else:
            lines.append(f"- {item}")
    lines.append(
        "Rewrite using ONLY the columns listed above for each table. If a value "
        "the question needs has no backing column on the table you chose, it may "
        "live on a DIFFERENT slice table — re-read the slice and use that table "
        f"instead. Slice tables: {', '.join(all_table_names)}."
    )
    return "\n".join(lines)
