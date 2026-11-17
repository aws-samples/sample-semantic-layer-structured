"""Build-time primary-key DISCOVERY by verified uniqueness probe.

Why this module exists
----------------------
Ontop treats an OBDA mapping's subject template as ROW IDENTITY: every property
access joins on it. Choosing the wrong column does not merely mislabel rows — it
turns each access into a self-join on a non-key column and silently returns wrong
answers. On the 2026-08-09 BIRD run the old name-pattern rule (``id`` > ``*_id`` >
``pk`` > first-alphabetical) put identity on value columns — ``aboutme`` (a
free-text bio), ``amount``, ``avgscrmath`` — in roughly 42% of executed rows,
which scored EX 0.0500 against 0.1313 for rows with clean SQL.

The consumer side (``lambda/ontop-translate`` ``OntologyMappings``) now prefers a
key the ontology DECLARES as ``mappings[classIri].primaryKey``. This module is
the PRODUCER of that declaration.

Why a probe, and not the catalog
--------------------------------
There is no key metadata to read. Glue's ``get_table`` returns no primary-key
field, and S3 Tables / Iceberg expose none through the federated catalog either —
Iceberg has an ``identifier-field-ids`` concept but our CTAS-materialized tables
do not set it. The upstream SQLite files DO carry a PK flag (``PRAGMA
table_info`` column 5), but that is lost at ingest and exists only for the
benchmark corpora, not for a customer's real Athena tables. So the only source of
key knowledge available to a general build is the DATA, and the only sound way to
read it is to measure uniqueness.

That measurement is also exactly the evidence a ``uniqueConstraint`` needs. The
constraint is a licence for Ontop to DROP de-duplication, so it must never be
inferred from a column NAME — the old rule asserted it on every ``*_id`` subject
and was measurably false on many-side FK columns (``trans.account_id``,
``disp.account_id`` and ``order.account_id`` all repeat).

The uniqueness test
-------------------
A column is a key when, over the FULL table, ``COUNT(DISTINCT col) = COUNT(*)``
and it has no NULLs. Both halves are required: ``COUNT(DISTINCT)`` ignores NULLs,
so a column with one NULL and otherwise-distinct values would pass the count test
while being unusable as identity (Ontop cannot template a NULL into an IRI, and
those rows would vanish from the class).

When no single column qualifies we try PAIRS, which is the shape a junction table
needs (``enrolment(student_id, course_id)``) and precisely where a single-column
guess is most wrong.

Fail-loudly posture
-------------------
This module NEVER guesses. A probe that fails, times out, or finds nothing
unique returns no key at all, and the consumer falls back to its name heuristic
while logging loudly that row identity is a guess. Emitting an unverified
``primaryKey`` would be worse than emitting none: it would additionally license
Ontop to drop de-duplication on a column that repeats.
"""

from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Max aggregate expressions per probe query. A wide table (100+ columns) would
# otherwise build one enormous SELECT; batching keeps each query modest while
# still covering EVERY column (we do not sample columns — a skipped column is a
# key we would silently fail to find).
MAX_AGGREGATES_PER_QUERY: int = 60

# Max candidate columns to consider for a COMPOSITE (pair) key. Pairs grow
# quadratically, so we only pair the most-selective columns. 6 candidates = 15
# pairs, which fits in a single probe query.
MAX_COMPOSITE_CANDIDATES: int = 6

# A probe seam takes SQL and returns row dicts (column label -> value), matching
# what ``main._run_athena_query`` yields in its "rows" field.
RunProbe = Callable[[str], List[Dict[str, object]]]


def _quote(identifier: str) -> str:
    """Double-quote a SQL identifier, escaping any embedded double quote.

    Args:
        identifier: A table or column name sourced from the Glue catalog.

    Returns:
        The identifier wrapped in double quotes, safe to interpolate into SQL.
    """
    return '"' + identifier.replace('"', '""') + '"'


def _alias(index: int, kind: str) -> str:
    """Build a positional result alias for one column's probe aggregate.

    Aliases are positional (``d0``/``n0``) rather than name-derived because a
    physical column name may be mixed-case, over-long, or clash with another
    column once Athena lower-cases result labels — any of which would make the
    result unreadable by name.

    Args:
        index: The candidate column's index within the probed batch.
        kind: ``"d"`` for the distinct count, ``"n"`` for the non-null count.

    Returns:
        The result-set alias, e.g. ``"d3"``.
    """
    return f"{kind}{index}"


def build_single_column_probe_sql(
    *, database: str, table: str, columns: Sequence[str]
) -> str:
    """Build ONE query measuring row count, distinct count and non-null count.

    Emits ``COUNT(*)`` once plus two aggregates per candidate column, so a single
    full-table scan answers uniqueness for every column in the batch. Scanning
    once for many columns is what makes probing every column affordable.

    Args:
        database: Glue/Athena database holding the table.
        table: Physical table name.
        columns: Candidate column names, in the order the aliases index them.

    Returns:
        The probe SQL. ``total`` is the row count; ``d<i>``/``n<i>`` are the
        distinct and non-null counts for ``columns[i]``.

    Raises:
        ValueError: ``columns`` is empty — an aggregate-less probe would report
            uniqueness for nothing and silently yield "no key found".
    """
    if not columns:
        raise ValueError(
            "build_single_column_probe_sql requires at least one candidate column; "
            "an empty probe would be indistinguishable from 'no key exists'."
        )
    selects: List[str] = ["COUNT(*) AS total"]
    for index, column in enumerate(columns):
        quoted = _quote(column)
        selects.append(f"COUNT(DISTINCT {quoted}) AS {_alias(index, 'd')}")
        # COUNT(col) counts NON-NULL rows; comparing it to COUNT(*) detects NULLs
        # without a separate predicate.
        selects.append(f"COUNT({quoted}) AS {_alias(index, 'n')}")
    # Identifiers come from the Glue catalog and are double-quoted, not user free-text.
    return (  # nosec B608
        "SELECT " + ", ".join(selects)
        + f" FROM {_quote(database)}.{_quote(table)}"
    )


def build_pair_probe_sql(
    *, database: str, table: str, pairs: Sequence[Tuple[str, str]]
) -> str:
    """Build ONE query measuring the distinct count of each candidate column PAIR.

    Counts distinct values of a separator-joined concatenation rather than
    ``COUNT(DISTINCT ROW(a, b))``: the row form is not portable across the
    Athena engine versions this project targets. The separator is the ASCII unit
    separator (0x1F), which cannot occur in benchmark or catalog data, so
    ``('a', 'bc')`` and ``('ab', 'c')`` cannot collide into one value — a
    collision would make a non-unique pair look unique.

    Only pairs whose BOTH columns are already known null-free are worth passing
    here: concatenation with a NULL yields NULL, which ``COUNT(DISTINCT)`` then
    ignores, understating the distinct count and hiding a duplicate.

    Args:
        database: Glue/Athena database holding the table.
        table: Physical table name.
        pairs: Candidate ``(left, right)`` column pairs.

    Returns:
        The probe SQL. ``total`` is the row count; ``d<i>`` is the distinct count
        of ``pairs[i]``.

    Raises:
        ValueError: ``pairs`` is empty.
    """
    if not pairs:
        raise ValueError(
            "build_pair_probe_sql requires at least one candidate pair; "
            "an empty probe would be indistinguishable from 'no key exists'."
        )
    selects: List[str] = ["COUNT(*) AS total"]
    for index, (left, right) in enumerate(pairs):
        joined = (
            f"CAST({_quote(left)} AS VARCHAR) || CHR(31) || "
            f"CAST({_quote(right)} AS VARCHAR)"
        )
        selects.append(f"COUNT(DISTINCT {joined}) AS {_alias(index, 'd')}")
    # Identifiers come from the Glue catalog and are double-quoted, not user free-text.
    return (  # nosec B608
        "SELECT " + ", ".join(selects)
        + f" FROM {_quote(database)}.{_quote(table)}"
    )


def _as_int(value: object) -> Optional[int]:
    """Coerce one Athena result cell to ``int``, or ``None`` when unusable.

    Athena returns every cell as a string (``VarCharValue``); a missing or
    non-numeric cell means the aggregate did not come back and MUST NOT be read
    as zero — zero would look like "no distinct values", which for an empty table
    would falsely satisfy the uniqueness test.

    Args:
        value: The raw cell value.

    Returns:
        The parsed integer, or ``None`` when the cell is absent or non-numeric.
    """
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _batch(columns: Sequence[str], *, size: int) -> List[List[str]]:
    """Split candidate columns into batches sized for one probe query each.

    Args:
        columns: All candidate column names.
        size: Max aggregate expressions per query (two per column).

    Returns:
        The batches, preserving input order.
    """
    per_batch = max(1, size // 2)
    return [
        list(columns[i : i + per_batch]) for i in range(0, len(columns), per_batch)
    ]


def probe_unique_columns(
    *,
    database: str,
    table: str,
    columns: Sequence[str],
    run_probe: RunProbe,
) -> Tuple[List[str], List[str], Optional[int]]:
    """Measure which columns are verified-unique and which are null-free.

    Runs one query per batch of columns so EVERY column is measured. A batch
    whose query fails is skipped with its columns treated as unknown (never as
    unique) — a failed probe must not manufacture a key.

    Args:
        database: Glue/Athena database holding the table.
        table: Physical table name.
        columns: Candidate column names in catalog order.
        run_probe: Query-executor seam returning row dicts.

    Returns:
        ``(unique_columns, null_free_columns, row_count)``. ``unique_columns``
        are null-free AND fully distinct over the whole table, in catalog order.
        ``null_free_columns`` is the wider set eligible for a composite key.
        ``row_count`` is the measured table row count, or ``None`` if no batch
        succeeded.

    Raises:
        ValueError: ``columns`` is empty.
    """
    if not columns:
        raise ValueError("probe_unique_columns requires at least one candidate column")

    unique: List[str] = []
    null_free: List[str] = []
    row_count: Optional[int] = None

    for batch in _batch(columns, size=MAX_AGGREGATES_PER_QUERY):
        sql = build_single_column_probe_sql(
            database=database, table=table, columns=batch
        )
        rows = run_probe(sql)
        if not rows:
            # No result row: the probe did not answer. Leave this batch unknown.
            continue
        row = rows[0]
        total = _as_int(row.get("total"))
        if total is None:
            continue
        row_count = total
        # An EMPTY table cannot evidence a key: every count is 0, so 0 == 0 would
        # mark every column unique. Refuse rather than declare a key on no data.
        if total == 0:
            continue
        for index, column in enumerate(batch):
            distinct = _as_int(row.get(_alias(index, "d")))
            non_null = _as_int(row.get(_alias(index, "n")))
            if distinct is None or non_null is None:
                continue
            if non_null != total:
                # Has NULLs. Not usable as identity: Ontop cannot template NULL
                # into an IRI, so those rows would disappear from the class.
                continue
            null_free.append(column)
            if distinct == total:
                unique.append(column)

    return unique, null_free, row_count


def _composite_candidates(
    *, null_free: Sequence[str], unique: Sequence[str]
) -> List[Tuple[str, str]]:
    """Choose the column PAIRS worth probing for a composite key.

    Excludes columns that are already unique on their own (a pair containing one
    is trivially unique but a strictly worse key — it drags an extra column into
    every join). Pairs the first ``MAX_COMPOSITE_CANDIDATES`` remaining null-free
    columns in catalog order.

    Args:
        null_free: Columns verified to contain no NULLs.
        unique: Columns already verified unique on their own.

    Returns:
        The candidate pairs, in a deterministic order.
    """
    already_unique = set(unique)
    pool = [c for c in null_free if c not in already_unique][:MAX_COMPOSITE_CANDIDATES]
    pairs: List[Tuple[str, str]] = []
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            pairs.append((pool[i], pool[j]))
    return pairs


def probe_unique_pair(
    *,
    database: str,
    table: str,
    pairs: Sequence[Tuple[str, str]],
    run_probe: RunProbe,
) -> Optional[Tuple[str, str]]:
    """Return the first candidate pair verified unique over the whole table.

    Args:
        database: Glue/Athena database holding the table.
        table: Physical table name.
        pairs: Candidate pairs to measure (both columns must be null-free).
        run_probe: Query-executor seam returning row dicts.

    Returns:
        The first pair whose distinct count equals the row count, or ``None``
        when no pair qualifies or the probe did not answer.
    """
    if not pairs:
        return None
    sql = build_pair_probe_sql(database=database, table=table, pairs=pairs)
    rows = run_probe(sql)
    if not rows:
        return None
    row = rows[0]
    total = _as_int(row.get("total"))
    # As with single columns, an empty table evidences nothing.
    if total is None or total == 0:
        return None
    for index, pair in enumerate(pairs):
        distinct = _as_int(row.get(_alias(index, "d")))
        if distinct is not None and distinct == total:
            return pair
    return None


def discover_primary_key(
    *,
    database: str,
    table: str,
    columns: Sequence[str],
    run_probe: RunProbe,
) -> List[str]:
    """Discover a table's primary key by verified uniqueness, or return no key.

    Order of preference:

    1. A single verified-unique, null-free column. Where several qualify, the
       FIRST in catalog order wins — a deterministic tie-break, so the same table
       yields the same key on every rebuild (a key that changed between builds
       would silently change every subject IRI in the layer).
    2. A verified-unique null-free PAIR, for junction tables with no single key.
    3. Nothing. The consumer then falls back to its name heuristic and logs that
       row identity is a guess.

    Args:
        database: Glue/Athena database holding the table.
        table: Physical table name.
        columns: The table's columns in catalog order.
        run_probe: Query-executor seam returning row dicts.

    Returns:
        The key's bare column names in key order — one entry for a simple key,
        two for a composite, or an EMPTY list when no key could be verified.

    Raises:
        ValueError: ``columns`` is empty.
    """
    unique, null_free, _row_count = probe_unique_columns(
        database=database, table=table, columns=columns, run_probe=run_probe
    )
    if unique:
        return [unique[0]]

    pairs = _composite_candidates(null_free=null_free, unique=unique)
    if not pairs:
        return []
    pair = probe_unique_pair(
        database=database, table=table, pairs=pairs, run_probe=run_probe
    )
    return list(pair) if pair else []
