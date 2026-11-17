"""Deterministic relationship (FK) inference for the Hybrid thin VKG.

Why this module exists
----------------------
A Hybrid layer's structural graph is built with NO model in the loop: the VKG
phase reads the Glue catalog and emits classes + datatype properties. That path
never produced a single ``owl:ObjectProperty``, so every Hybrid layer's
"Relationships" view read *No relationships found* and the query side had no
declared edges to join on — while the equivalent non-Hybrid layer gets its edges
from the ontology agent's Phase-1 ``fk_hints`` (a model reads the schema and
names the targets).

There is no catalog metadata to read instead. Glue's ``get_table`` returns no
foreign keys, and S3 Tables / Iceberg expose none through the federated catalog.
So the only inputs available to a deterministic build are the table names and the
column names of the tables the user selected for THIS layer, which is what this
module works from.

The two rules
-------------
Both rules operate on TOKENIZED names (``setCode`` -> ``["set", "code"]``), never
on raw substrings — a substring rule matches ``valid``/``hybrid`` as "ends in id"
and invents edges out of value columns.

One wrinkle makes tokenizing alone insufficient: the Glue/Iceberg catalog folds
column names to lower case, so a camelCase source schema arrives with its word
boundaries GONE (``setCode`` -> ``setcode``, a single token). Unbroken runs are
therefore re-segmented on an identifier suffix, guarded two different ways per
rule — see :data:`MIN_SEGMENT_LENGTH` and :data:`NO_SPLIT_TOKENS`.

1. **Reference rule (directed).** A column whose last token is an identifier
   token and whose remaining tokens name another table in the layer is a
   reference to that table: ``cards.setCode`` -> ``sets`` (matched singular /
   plural), grounded on the target's matching key column (``sets.code``). The
   target may be named by a SUFFIX of those tokens, so role-qualified names work
   too: ``flights.origin_airport_id`` -> ``airports``.

2. **Shared-identifier rule (symmetric).** The same identifier column name
   appearing in two or more of the layer's tables — and read as a reference by
   NEITHER of them — is a join key they have in common: ``cards.uuid`` /
   ``rulings.uuid`` / ``legalities.uuid``. Direction
   cannot be established from names alone (nothing in ``uuid`` says which table
   owns the value), so BOTH directions are emitted rather than guessing an owner
   — the honest reading is "these two tables join on this column".

   Bare surrogate names (``id``, ``pk``, ``rowid``, …) are excluded from this
   rule: they are each table's OWN key, not a shared one, and pairing them would
   connect every table in the layer to every other.

What this module deliberately does NOT do
-----------------------------------------
It does not assert uniqueness. An inferred edge is a claim that two columns
relate, not a claim that either side is a key — key declaration requires
measurement (see ``athena_probe.probe_unique_columns``) because a declared key
also licenses Ontop to drop de-duplication.

Copy-and-own: standard library only. No import from ``agents.shared`` or any
sibling agent package (enforced by ``tests/unit/test_hybrid_isolation.py``).
"""

import re
from typing import Dict, List, Sequence, Set, Tuple

# Tokens that mark a column as an IDENTIFIER when they are the column's LAST
# token. Deliberately conservative: ``no``/``num``/``number`` are omitted because
# ``phone_no``-style value columns share them across tables and would produce
# edges between unrelated tables.
IDENTIFIER_TOKENS: Set[str] = {
    "id", "ids", "uuid", "guid", "code", "codes", "key", "keys", "ref", "fk", "sk",
}

# Column names that are a table's OWN surrogate key rather than a shared join
# key. Eligible as a reference TARGET (rule 1) but never as a shared identifier
# (rule 2), where they would link every table to every other one.
OWN_KEY_NAMES: Set[str] = {
    "id", "pk", "key", "sk", "rowid", "row_id", "oid", "seq", "sequence", "index",
}

# Max tables allowed to share one identifier column before the shared-identifier
# rule gives up on it. A name shared by more tables than this is almost certainly
# a generic value column, and the symmetric emit would add n*(n-1) edges.
MAX_SHARED_TABLES: int = 6

# Identifier tokens that must NEVER be split out of an unbroken lowercase run
# (see :func:`split_candidates`). Each is the tail of ordinary English words —
# ri/sk, ta/sk, de/sk, di/sk — so segmenting on them manufactures identifiers out
# of value columns. They are still honoured when a real boundary spells them out
# (``order_sk``).
NO_SPLIT_TOKENS: Set[str] = {"sk", "fk", "ref"}

# Minimum remainder length when segmenting an unbroken run for the SHARED rule.
# Rule 1 validates its own segmentation (the remainder must name a table in the
# layer), so it needs no floor and can accept ``setcode`` -> ``set`` + ``code``.
# Rule 2 has nothing to validate against, so it requires a substantial remainder:
# ``multiverseid`` -> ``multiverse`` is an identifier, ``valid`` -> ``val`` is not.
MIN_SEGMENT_LENGTH: int = 4


def tokenize(name: str) -> List[str]:
    """Split a table or column name into lower-cased word tokens.

    Handles snake_case, kebab-case, camelCase, PascalCase and acronym runs, so
    ``setCode``, ``set_code`` and ``SetCODE`` all tokenize identically.

    Args:
        name: A raw table or column name from the Glue catalog.

    Returns:
        list[str]: The lower-cased tokens in source order (empty for a name with
            no alphanumeric characters).
    """
    tokens: List[str] = []
    for part in re.split(r"[^0-9A-Za-z]+", name or ""):
        if not part:
            continue
        # Split camelCase/PascalCase while keeping acronym runs (UUID) whole.
        for token in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", part):
            tokens.append(token.lower())
    return tokens


def singular(token: str) -> str:
    """Return the naive singular form of one lower-cased token.

    Table names are pluralized inconsistently (``sets``, ``foreign_data``,
    ``legalities``), so reference matching normalizes both sides through this
    function. It is intentionally naive — it only has to be CONSISTENT, since it
    is applied to both the column prefix and the candidate table name.

    Args:
        token: A single lower-cased token.

    Returns:
        str: The singularized token (unchanged when no rule applies).
    """
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("sses", "shes", "ches", "xes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 1:
        return token[:-1]
    return token


def normalize_name(name: str) -> str:
    """Return the comparison key for a table or column name.

    Tokens are singularized and re-joined, so ``sets`` and ``set`` share a key
    and ``setCode`` and ``set_code`` do too.

    Args:
        name: A raw table or column name.

    Returns:
        str: The normalized comparison key (``''`` when the name has no tokens).
    """
    return "".join(singular(token) for token in tokenize(name))


def _segment_suffix(token: str) -> str:
    """Return the identifier token an unbroken lowercase run ends with.

    The Glue/Iceberg catalog folds column names to lower case, which DESTROYS the
    camelCase boundaries the source schema had: ``setCode`` arrives as
    ``setcode`` and tokenizes to a single token. Without re-segmenting it, every
    camelCase foreign key in a catalog-backed layer is invisible.

    Args:
        token: One lower-cased token (the column's last).

    Returns:
        str: The longest identifier token the run ends with, leaving a non-empty
            remainder; ``''`` when there is none. Tokens in
            :data:`NO_SPLIT_TOKENS` are never returned.
    """
    for key in sorted(IDENTIFIER_TOKENS, key=len, reverse=True):
        if key in NO_SPLIT_TOKENS:
            continue
        if token.endswith(key) and len(token) > len(key):
            return key
    return ""


def split_candidates(name: str) -> List[Tuple[List[str], str]]:
    """Return every ``(prefix_tokens, key_token)`` reading of a column name.

    The explicit reading (a real token boundary, ``set_code`` / ``setCode``) comes
    first, then the segmented reading of an unbroken run (``setcode``). Callers try
    them in order and keep the first that grounds on a real table, so an explicit
    boundary always wins over a guessed one.

    Args:
        name: A raw column name.

    Returns:
        list[tuple[list[str], str]]: Readings, best first. Empty when the name
            carries no identifier token at all.
    """
    tokens = tokenize(name)
    if not tokens:
        return []
    readings: List[Tuple[List[str], str]] = []
    # A reference needs BOTH a prefix and a key token: a bare `uuid` names no
    # target, so `len(tokens) >= 2` here and a segmented reading below.
    if len(tokens) >= 2 and tokens[-1] in IDENTIFIER_TOKENS:
        readings.append((tokens[:-1], tokens[-1]))
    key = _segment_suffix(tokens[-1])
    if key:
        readings.append((tokens[:-1] + [tokens[-1][: -len(key)]], key))
    return readings


def _is_identifier_column(name: str) -> bool:
    """Whether a column name looks like an identifier (for the SHARED rule).

    Args:
        name: A raw column name.

    Returns:
        bool: True when the final token IS an identifier token (``uuid``,
            ``customer_id``), or is an unbroken run ending in one and leaving a
            remainder of at least :data:`MIN_SEGMENT_LENGTH` characters
            (``multiverseid`` yes, ``valid`` no — see that constant).
    """
    tokens = tokenize(name)
    if not tokens:
        return False
    if tokens[-1] in IDENTIFIER_TOKENS:
        return True
    key = _segment_suffix(tokens[-1])
    return bool(key) and len(tokens[-1]) - len(key) >= MIN_SEGMENT_LENGTH


def _table_index(columns_by_table: Dict[str, Sequence[str]]) -> Dict[str, str]:
    """Index the layer's tables by normalized name for reference matching.

    Args:
        columns_by_table: Mapping of physical table name -> its column names.

    Returns:
        dict[str, str]: normalized table name -> physical table name. A
            normalized name claimed by two tables (e.g. ``set`` and ``sets`` both
            present) is DROPPED: the reference is ambiguous and inventing one of
            the two targets would silently mis-ground every edge through it.
    """
    index: Dict[str, str] = {}
    ambiguous: Set[str] = set()
    for table in columns_by_table:
        key = normalize_name(table)
        if not key:
            continue
        if key in index:
            ambiguous.add(key)
            continue
        index[key] = table
    for key in ambiguous:
        index.pop(key, None)
    return index


def _resolve_target_column(
    *,
    source_column: str,
    target_columns: Sequence[str],
    target_table: str,
    key_token: str,
) -> str:
    """Find the column in the target table that a reference column points at.

    Preference order (first match wins, so the result is deterministic):

    1. the identically-named column (``sets.set_code`` for ``cards.set_code``);
    2. the bare key token (``sets.code`` for ``cards.setCode``);
    3. the target-name-qualified key (``sets.set_code`` for ``cards.setCode``);
    4. the target's own ``id`` column, but only for an ``*_id`` reference.

    Args:
        source_column: The referencing column's name.
        target_columns: The candidate target table's column names.
        target_table: The candidate target table's physical name.
        key_token: The reference column's final (identifier) token.

    Returns:
        str: The matched target column name, or ``''`` when nothing matches — an
            unmatched reference is dropped rather than grounded on a guess.
    """
    by_key = {normalize_name(column): column for column in target_columns}
    candidates = [
        normalize_name(source_column),
        singular(key_token),
        normalize_name(target_table) + singular(key_token),
    ]
    if key_token in ("id", "ids"):
        candidates.append("id")
    for candidate in candidates:
        if candidate in by_key:
            return by_key[candidate]
    return ""


def _match_target_table(
    *, prefix_tokens: Sequence[str], index: Dict[str, str]
) -> str:
    """Find the layer table a reference column's prefix tokens name.

    The whole prefix is tried first, then progressively shorter SUFFIXES of it:
    ``origin_airport_id`` -> ``originairport`` (no match) -> ``airport`` ->
    ``airports``. Role-qualified FK names like ``origin_airport_id`` /
    ``ship_to_customer_id`` are the common case in real schemas, and a
    whole-prefix-only match silently drops every one of them.

    Only SUFFIXES are tried, never arbitrary token subsets: the head tokens of an
    FK name are the ROLE (``origin``, ``ship_to``) and the tail is the target, so
    dropping from the front is the only direction that preserves the reading.

    Args:
        prefix_tokens: The column's tokens with the trailing identifier token
            already removed.
        index: normalized table name -> physical table name (see
            :func:`_table_index`).

    Returns:
        str: The physical target table name, or ``''`` when no suffix matches.
    """
    for start in range(len(prefix_tokens)):
        key = "".join(singular(token) for token in prefix_tokens[start:])
        if key in index:
            return index[key]
    return ""


def _pascal(name: str) -> str:
    """Render a table or column name as PascalCase for a property-name suffix.

    Args:
        name: A raw table or column name.

    Returns:
        str: The PascalCase form (``set_translations`` -> ``SetTranslations``).
    """
    return "".join(token.capitalize() for token in tokenize(name))


def infer_joins(
    columns_by_table: Dict[str, Sequence[str]],
) -> List[Dict[str, str]]:
    """Infer the layer's relationship edges from its table and column names.

    Applies the reference rule first, then the shared-identifier rule for column
    names the reference rule did not already resolve. Property names are assigned
    last so a table referencing the same target through two different columns gets
    two distinct property IRIs instead of one silently overwriting the other.

    Args:
        columns_by_table: Mapping of physical table name -> its column names, for
            EVERY table in the layer. Inference is scoped to this set: an edge to
            a table the layer does not contain has no class to point at.

    Returns:
        list[dict]: Edges as ``{"source_table", "source_column", "target_table",
            "target_column", "property_name", "rule"}``, sorted by
            (source_table, property_name) so a rebuild of an unchanged layer emits
            byte-identical N-Quads. ``rule`` is ``'reference'`` or ``'shared'``.
    """
    index = _table_index(columns_by_table)
    # (source_table, source_column, target_table, target_column) -> rule name.
    edges: Dict[Tuple[str, str, str, str], str] = {}
    # Columns rule 1 read as a reference to another table. Rule 2 must skip them:
    # a column that demonstrably points somewhere else is a foreign key, not a
    # shared identity, so pairing two such columns (cards.setCode <->
    # set_translations.setCode) only restates a two-hop path through their common
    # target and can never be a real join, since neither side is unique.
    referencing: Set[Tuple[str, str]] = set()

    # --- Rule 1: reference columns naming another table in the layer ---------
    for table, columns in columns_by_table.items():
        for column in columns:
            for prefix_tokens, key_token in split_candidates(column):
                target_table = _match_target_table(
                    prefix_tokens=prefix_tokens, index=index
                )
                if not target_table or target_table == table:
                    continue
                target_column = _resolve_target_column(
                    source_column=column,
                    target_columns=columns_by_table[target_table],
                    target_table=target_table,
                    key_token=key_token,
                )
                if not target_column:
                    continue
                edges[(table, column, target_table, target_column)] = "reference"
                referencing.add((table, column))
                # The first reading that grounds wins; a segmented reading must
                # not add a second target for the same column.
                break

    # --- Rule 2: one identifier column name shared by several tables --------
    shared: Dict[str, List[Tuple[str, str]]] = {}
    for table, columns in columns_by_table.items():
        for column in columns:
            key = normalize_name(column)
            if key in OWN_KEY_NAMES or not _is_identifier_column(column):
                continue
            if (table, column) in referencing:
                continue
            shared.setdefault(key, []).append((table, column))
    for key, holders in shared.items():
        tables = {table for table, _column in holders}
        if len(tables) < 2 or len(tables) > MAX_SHARED_TABLES:
            continue
        for source_table, source_column in holders:
            for target_table, target_column in holders:
                if source_table == target_table:
                    continue
                edges.setdefault(
                    (source_table, source_column, target_table, target_column),
                    "shared",
                )

    # --- Property naming (done last: needs the full edge set) ---------------
    pairs: Dict[Tuple[str, str], int] = {}
    for source_table, _source_column, target_table, _target_column in edges:
        pairs[(source_table, target_table)] = pairs.get(
            (source_table, target_table), 0
        ) + 1

    result: List[Dict[str, str]] = []
    for (source_table, source_column, target_table, target_column), rule in edges.items():
        name = f"has{_pascal(target_table)}"
        # Two columns of the same table referencing the same target must not
        # collapse onto one property IRI — qualify BOTH by the source column.
        if pairs[(source_table, target_table)] > 1:
            name = f"{name}Via{_pascal(source_column)}"
        result.append(
            {
                "source_table": source_table,
                "source_column": source_column,
                "target_table": target_table,
                "target_column": target_column,
                "property_name": name,
                "rule": rule,
            }
        )
    result.sort(key=lambda edge: (edge["source_table"], edge["property_name"]))
    return result


def key_columns_by_table(edges: Sequence[Dict[str, str]]) -> Dict[str, List[str]]:
    """Group the edges' TARGET columns per target table (key probe candidates).

    Ontop unifies an FK's object template with the target class's SUBJECT
    template structurally, so an edge only becomes a real SQL join when the
    target's declared key IS the referenced column. These are therefore exactly
    the columns worth measuring for uniqueness — see
    ``athena_probe.probe_unique_columns``.

    Args:
        edges: Edges as returned by :func:`infer_joins`.

    Returns:
        dict[str, list[str]]: target table -> referenced column names, sorted and
            de-duplicated.
    """
    candidates: Dict[str, Set[str]] = {}
    for edge in edges:
        candidates.setdefault(edge["target_table"], set()).add(edge["target_column"])
    return {table: sorted(columns) for table, columns in candidates.items()}
