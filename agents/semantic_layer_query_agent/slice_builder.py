"""Recall Phase 2 (Hybrid): build a JSON schema slice with few-shot injection.

Copy-and-own of the RAG agent's ``rag_slice_builder`` + ``markdown_slice_parser``
recall phase, re-shaped for the Hybrid agent. The slice JSON carries the same
schema keys the RAG slice does — ``tables / columns / joins / acord_paths /
query_patterns`` — and ADDITIONALLY a ``few_shots`` key holding the sample-SQL /
glossary exemplars the Hybrid agent injects from the KB. Those few-shots are what
lets the SQL generator pattern-match a proven query against the recalled schema.

Two injected seams keep the builder unit-testable without AWS:

* ``kb_retrieve(question, namespace) -> List[Dict]`` — returns the few-shot
  exemplars (sample-SQL / glossary chunks) for the question.
* ``chunks_lookup(table_ids, namespace) -> {table_id: markdown_body}`` — OPTIONAL
  real KB chunk source. When supplied, each candidate's markdown doc is parsed
  (via the copied parsers below) to populate columns / joins / acord_paths /
  query_patterns. When ``None`` (unit tests), the slice still carries the tables
  and few-shots so the ``few_shots`` contract holds without a chunk store.

The markdown parsers are copied inline (not imported) — the sibling
``metadata_query_agent.tier2.markdown_slice_parser`` is off-limits under the
Hybrid copy-and-own isolation rule. Standard library ONLY beyond that.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- copied markdown parsers (see metadata_agent doc template) --------------
# One ``{catalog}.{database}.{table}`` markdown doc per table is ingested into the
# KB. These best-effort parsers pull the structured sections; a missing section
# returns empty rather than raising, so the slice builder keeps working on docs
# authored before the format stabilised.

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _section(*, md: str, title: str) -> Optional[str]:
    """Return the body of ``## {title}`` or ``None`` when the section is absent.

    Matching is case-insensitive on the title; the body is everything between this
    heading and the next ``##`` (or end of document).

    Args:
        md: The full markdown document body.
        title: The section heading text to locate (without the ``##``).

    Returns:
        The stripped section body, or ``None`` when no such heading exists.
    """
    target = title.strip().lower()
    matches = list(_SECTION_RE.finditer(md))
    for idx, m in enumerate(matches):
        if m.group(1).strip().lower() == target:
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
            return md[start:end].strip()
    return None


# Public alias: ``kb_table_index`` parses the SAME KB documents to build the
# topic-router recall text, and must split their sections identically. Exported
# so that module needs no private-name import and the two cannot drift apart.
section = _section


def _parse_columns(*, md: str, table_id: str) -> List[Dict[str, str]]:
    """Parse the ``## Columns`` markdown table into column dicts.

    Args:
        md: The full markdown document body.
        table_id: The ``{database}.{table}`` id the columns belong to.

    Returns:
        ``[{table_id, name, type, description}, ...]``; header / divider / malformed
        rows are skipped.
    """
    body = _section(md=md, title="Columns")
    if not body:
        return []
    rows: List[Dict[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # Skip the markdown header divider row "|---|---|---|".
        if set(line.replace("|", "").strip()) <= {"-", " ", ":"}:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name, ctype, desc = cells[0], cells[1], cells[2]
        # The header row carries the literal "Column" / "Type" labels.
        if name.lower() == "column" and ctype.lower() == "type":
            continue
        if not name:
            continue
        rows.append({
            "table_id": table_id, "name": name, "type": ctype, "description": desc,
        })
    return rows


# A fenced code block, ```sql-tagged or bare. The creation agent puts each join's
# real SQL in one of these under the ``## Reference Tables`` bullet it belongs to.
_FENCE_RE = re.compile(r"```(?:[a-z]*)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# A single-backtick code span containing a JOIN. Several documents skip the fence
# and put the whole clause inline under a nested bullet, e.g.
# ``- `JOIN holding h ON life_detail.holding_id = h.holding_id```.
_INLINE_SQL_RE = re.compile(r"`([^`\n]*\bJOIN\b[^`\n]*)`", re.IGNORECASE)

# One JOIN clause: everything from ``JOIN`` up to the next JOIN or trailing clause.
# Non-greedy with a lookahead terminator so a multi-line ``ON a AND b`` stays whole.
_JOIN_CLAUSE_RE = re.compile(
    r"\bJOIN\b.*?(?=\bJOIN\b|\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|\bUNION\b|$)",
    re.DOTALL | re.IGNORECASE,
)

# ``FROM|JOIN <table> [AS] [alias]``. The optional alias group is validated against
# _ALIAS_STOPWORDS below, since an un-aliased ``JOIN holding ON ...`` would
# otherwise capture the keyword ``ON`` as the alias.
_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)\s*(?:AS\s+)?([A-Za-z_]\w*)?",
    re.IGNORECASE,
)

# The ON clause of a single JOIN, i.e. everything after the ``ON`` keyword.
_ON_CLAUSE_RE = re.compile(r"\bON\b(.*)$", re.DOTALL | re.IGNORECASE)

# Split an ON clause into its individual conditions.
_COND_SPLIT_RE = re.compile(r"\b(?:AND|OR)\b", re.IGNORECASE)

# A ``qualifier.column`` reference. Deliberately looser than a full
# ``a.x = b.y`` equality so a key transform such as
# ``REPLACE(dl.party_id, 'PARTY#', '') = p.party_id`` still yields both sides —
# those prefixed-vs-native joins are exactly the ones a generator gets wrong unaided.
_QUAL_COL_RE = re.compile(r"\b(\w+)\.(\w+)\b")

# Tokens that can follow a table name but are NOT an alias.
_ALIAS_STOPWORDS = frozenset({
    "on", "using", "where", "group", "order", "limit", "union", "and", "or",
    "join", "inner", "left", "right", "full", "outer", "cross", "as", "having",
})


def _bare_table(name: str) -> str:
    """Reduce a possibly schema-qualified table reference to its bare name.

    Parameters:
        name: A table reference such as ``normalized.party`` or ``party``.
    Returns:
        str: The lowercased final dot-segment (``party``).
    """
    return name.rsplit(".", 1)[-1].lower()


def _sql_alias_map(*, sql: str) -> Dict[str, str]:
    """Map every alias and bare table name in ``sql`` to its bare table name.

    Needed because the ON clause is written in aliases (``lp.holding_id =
    h.holding_id``) while the edge must report which side belongs to THIS table.

    Parameters:
        sql: A SQL snippet (one fenced block's body).
    Returns:
        Dict[str, str]: Lowercased alias/table token -> bare table name.
    """
    mapping: Dict[str, str] = {}
    for raw_table, raw_alias in _TABLE_REF_RE.findall(sql):
        table = _bare_table(raw_table)
        mapping[table] = table
        alias = (raw_alias or "").lower()
        if alias and alias not in _ALIAS_STOPWORDS:
            mapping[alias] = table
    return mapping


def _parse_fenced_joins(*, body: str, table_id: str) -> List[Dict[str, str]]:
    """Extract join edges from the fenced SQL blocks in a Reference Tables body.

    This is the shape ``semantic_layer_creation_agent.query_prompts``'s
    ``KB_DOCUMENT_FORMAT`` actually emits: an arrow bullet naming the FK, then the
    runnable JOIN in a following fenced block. The one-line bullet parser matches
    neither, which is why the live 40-document hybrid corpus yielded joins for only
    3 documents while all 502 of its columns parsed.

    A composite key (``ON a.x = b.x AND a.y = b.y``) yields ONE EDGE PER KEY PAIR:
    a generator told about only half a bridge-table key produces a fan-out join.

    Orientation comes from the ON clause's qualifiers, NOT from which side of the
    ``JOIN`` keyword a table sits on. A parent/lookup table (``coverage_product``,
    ``admin_codes``) has no outbound FK, so its document shows only INBOUND joins —
    the child in ``FROM``, this table after ``JOIN``. Assuming "the joined table is
    the target" reads those as self-joins and drops every edge, which is what left
    those documents at zero joins.

    Parameters:
        body: The ``## Reference Tables`` section body.
        table_id: The ``{database}.{table}`` id these joins originate from.
    Returns:
        List[Dict[str, str]]: ``[{from, to, from_col, to_col, sql}]``. A bullet whose
            prose describes a relationship with no JOIN contributes nothing — there is
            no key to report, and inventing one would hand the generator a join it
            cannot write.
    """
    base: str = _bare_table(table_id)
    edges: List[Dict[str, str]] = []
    # Fenced blocks first (document order), then inline spans, so a duplicate edge
    # described both ways keeps the fenced version's fuller SQL.
    blocks: List[str] = _FENCE_RE.findall(body) + _INLINE_SQL_RE.findall(body)
    for block in blocks:
        alias_map: Dict[str, str] = _sql_alias_map(sql=block)
        # Seed the base table under its own name: an inline span often has NO
        # ``FROM`` clause and refers to this table by name (``life_detail.holding_id``),
        # so the FROM/JOIN scan alone would never resolve that qualifier.
        alias_map.setdefault(base, base)
        for clause in _JOIN_CLAUSE_RE.findall(block):
            on_match = _ON_CLAUSE_RE.search(clause)
            if on_match is None:
                # A ``JOIN ... USING(col)`` or bare cross join names no key pair.
                continue
            # Collapse the (often multi-line) clause so the slice JSON stays compact
            # and the generator can copy it verbatim — including any key transform.
            clause_sql: str = " ".join(clause.split())
            for condition in _COND_SPLIT_RE.split(on_match.group(1)):
                edge = _edge_from_condition(
                    condition=condition, base=base, alias_map=alias_map,
                    table_id=table_id, clause_sql=clause_sql,
                )
                if edge is not None:
                    edges.append(edge)
    return edges


def _edge_from_condition(
    *,
    condition: str,
    base: str,
    alias_map: Dict[str, str],
    table_id: str,
    clause_sql: str,
) -> Optional[Dict[str, str]]:
    """Turn one ON-clause condition into a join edge, or ``None`` when it isn't one.

    A condition qualifies only when its qualified column references resolve to
    EXACTLY TWO tables, one of them ``base``. That single rule rejects the three
    non-join shapes that appear in the corpus:

    * a literal filter (``ac.admin_code_type = 'STATE'``) — one table;
    * a self-join condition — one table;
    * a bridge-path example between two OTHER tables — ``base`` absent, so
      attributing the join here would give the generator a key this table lacks.

    Parameters:
        condition: One condition from the ON clause (already split on AND/OR).
        base: The bare name of the table the document describes.
        alias_map: Alias/table token -> bare table name, for this SQL block.
        table_id: The ``{database}.{table}`` id to record as ``from``.
        clause_sql: The whitespace-collapsed JOIN clause to record as ``sql``.
    Returns:
        Optional[Dict[str, str]]: The ``{from, to, from_col, to_col, sql}`` edge, or
            ``None`` when the condition names no cross-table key pair.
    """
    # {bare_table: first column referenced on it}. First-wins keeps the key column
    # rather than a later one from a wrapping function's extra arguments.
    by_table: Dict[str, str] = {}
    for qualifier, column in _QUAL_COL_RE.findall(condition):
        table = alias_map.get(qualifier.lower(), "")
        if table and table not in by_table:
            by_table[table] = column
    if len(by_table) != 2 or base not in by_table:
        return None
    target: str = next(name for name in by_table if name != base)
    return {
        "from": table_id, "to": target, "sql": clause_sql,
        "from_col": by_table[base], "to_col": by_table[target],
    }


def _parse_reference_joins(*, md: str, table_id: str) -> List[Dict[str, str]]:
    """Parse the ``## Reference Tables`` section into structured join edges.

    Handles BOTH KB document formats in play, because the hybrid agent reads
    documents from whichever writer built the layer:

    * the one-line bullet ``- `target`: JOIN target x ON t.col = x.col`` written by
      ``metadata_agent.prompt_builder`` (the shape these parsers were copied for);
    * the arrow bullet plus a following fenced ```sql`` block written by
      ``semantic_layer_creation_agent.query_prompts.KB_DOCUMENT_FORMAT``.

    Args:
        md: The full markdown document body.
        table_id: The ``{database}.{table}`` id these joins originate from.

    Returns:
        ``[{from, to, from_col, to_col, sql}]``, de-duplicated on
        ``(to, from_col, to_col)`` since the two formats can describe the same edge.
        A one-line bullet without an ON-clause yields empty ``from_col`` / ``to_col``.
    """
    body = _section(md=md, title="Reference Tables")
    if not body:
        return []
    edges: List[Dict[str, str]] = []
    bullet_re = re.compile(r"^-\s*`?([^`:\s]+)`?\s*:\s*(.+)$")
    on_re = re.compile(r"\bON\b\s+(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)", re.IGNORECASE)
    for raw in body.splitlines():
        line = raw.strip()
        m = bullet_re.match(line)
        if not m:
            continue
        target, sql = m.group(1).strip(), m.group(2).strip()
        # The metadata_agent template always writes ``JOIN <target> ...`` after the
        # colon, so requiring the target BY NAME in a JOIN is the precise test. It
        # rejects a prose bullet whose text merely contains the word "join"
        # ("- Inbound: any table that joins to `holding` can be related by ..."),
        # which otherwise became an edge to a nonexistent table called "Inbound" —
        # a fabricated join target handed to the generator as if it were real.
        if not re.search(rf"\bJOIN\s+`?{re.escape(target)}\b", sql, re.IGNORECASE):
            continue
        edge: Dict[str, str] = {
            "from": table_id, "to": target, "sql": sql,
            "from_col": "", "to_col": "",
        }
        on_match = on_re.search(sql)
        if on_match:
            # The alias on either side of ON is opaque, so report column names
            # directly (same column on both sides is the dominant pattern).
            edge["from_col"] = on_match.group(2)
            edge["to_col"] = on_match.group(4)
        edges.append(edge)
    edges.extend(_parse_fenced_joins(body=body, table_id=table_id))
    # The same edge can appear in both a bullet and its fenced block; keep the
    # first-seen occurrence so ordering stays document order.
    deduped: List[Dict[str, str]] = []
    seen: set = set()
    for edge in edges:
        key = (edge["to"], edge["from_col"], edge["to_col"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def _parse_acord_path(*, md: str) -> Optional[str]:
    """Return the ACORD path from ``## ACORD Source Path`` or ``None``.

    Args:
        md: The full markdown document body.

    Returns:
        The first non-empty line of the section, or ``None`` when absent.
    """
    body = _section(md=md, title="ACORD Source Path")
    if not body:
        return None
    for line in body.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _parse_query_patterns(*, md: str) -> List[str]:
    """Return the bullet entries under ``## Common Query Patterns``.

    Args:
        md: The full markdown document body.

    Returns:
        The list of bullet texts (``-`` or ``*`` bullets), or ``[]`` when absent.
    """
    body = _section(md=md, title="Common Query Patterns")
    if not body:
        return []
    out: List[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith(("- ", "* ")):
            out.append(line[2:].strip())
    return out


class SliceBuilder:
    """Build a JSON schema slice + injected few-shots within a token budget."""

    def __init__(self, *,
                 kb_retrieve: Callable[[str, str], List[Dict[str, Any]]],
                 judge_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
                 token_counter: Callable[[str], int], budget: int,
                 chunks_lookup: Optional[Callable[..., Dict[str, str]]] = None
                 ) -> None:
        """Construct the builder.

        Args:
            kb_retrieve: Callable ``kb_retrieve(question, namespace) -> [dict]``
                returning the few-shot exemplars (sample-SQL / glossary chunks) to
                inject into the slice's ``few_shots`` key.
            judge_fn: Callable deciding whether the slice answers the question;
                returns ``{sufficient: bool, missing: [table_id]}``.
            token_counter: Counts tokens of the serialized slice for budget
                enforcement.
            budget: Maximum tokens for the serialized slice.
            chunks_lookup: Optional callable ``chunks_lookup(table_ids, namespace)
                -> {table_id: markdown_body}`` giving the real KB chunk source used
                at runtime to parse columns / joins / acord_paths / query_patterns.
                ``None`` (the unit-test path) yields a slice with tables + few-shots
                only — the schema-detail keys stay present but empty.
        """
        self.kb_retrieve = kb_retrieve
        self.judge = judge_fn
        self.tokens = token_counter
        self.budget = budget
        self.chunks_lookup = chunks_lookup
        self._candidates: List[str] = []
        self._namespace: str = ""
        self._question: str = ""
        # Accumulated judge token usage across is_sufficient() calls; the workflow
        # rolls this into its running total.
        self.judge_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}

    def build(self, *, candidates: List[str], namespace: str, question: str) -> str:
        """Build the initial slice from recalled candidate table ids.

        Assembles the slice JSON with keys ``{tables, columns, joins, acord_paths,
        query_patterns, few_shots}``. ``tables`` come from ``candidates``; the
        schema-detail keys are parsed from each candidate's KB markdown chunk (when
        a ``chunks_lookup`` is configured); ``few_shots`` are injected verbatim from
        ``kb_retrieve(question, namespace)``.

        Args:
            candidates: Recalled ``{database}.{table}`` ids (router rank order).
            namespace: Semantic-layer namespace for the chunk / few-shot lookups.
            question: The NL question — forwarded to ``kb_retrieve`` for few-shots.

        Returns:
            The serialized (``json.dumps``) slice, shrunk to fit ``budget``.
        """
        self._candidates = list(candidates)
        self._namespace = namespace
        self._question = question
        return self._fit(self._slice_dict())

    def is_sufficient(self, *, slice_text: str,
                      question: str) -> Tuple[bool, Optional[List[str]]]:
        """Ask the judge whether ``slice_text`` answers ``question``.

        Args:
            slice_text: The serialized slice to evaluate.
            question: The NL question the slice must answer.

        Returns:
            ``(sufficient, missing)`` — the judge's boolean verdict and its list of
            missing table / ``table.column`` ids (or ``None`` when the judge omits
            it).
        """
        verdict = self.judge({"slice": slice_text, "question": question})
        usage = verdict.get("usage") or {}
        # Accumulate every usage key the judge reports so the running total stays
        # consistent with the in/out breakdown.
        for key, value in usage.items():
            self.judge_usage[key] = self.judge_usage.get(key, 0) + int(value or 0)
        return bool(verdict.get("sufficient")), verdict.get("missing")

    def expand(self, *, slice_text: str, missing: List[str]) -> str:
        """Re-build the slice widened by the judge-requested ``missing`` tables.

        Each ``missing`` entry is reduced to its ``{database}.{table}`` id (a
        ``table.column`` request keeps its first two dot-segments). When a
        ``chunks_lookup`` is configured, an entry is folded in ONLY if it resolves
        to a fetchable chunk — so a hallucinated column on a nonexistent table
        cannot pollute ``tables[]``. Without a chunk source (unit-test path), the
        resolved table ids are folded in directly.

        Args:
            slice_text: The prior slice (unused beyond signature symmetry — the
                builder holds the candidate state it re-serializes from).
            missing: Judge-reported missing identifiers (table or ``table.column``).

        Returns:
            The re-serialized, budget-fitted slice including the new tables.
        """
        new_tables = self._resolve_missing_to_tables(missing)
        if new_tables:
            self._candidates = list(dict.fromkeys(self._candidates + new_tables))
        else:
            logger.info("slice.expand: no fetchable table in missing=%s; "
                        "slice unchanged", missing)
        return self._fit(self._slice_dict())

    @staticmethod
    def _reduce_to_table_id(entry: str) -> str:
        """Reduce a judge ``missing`` entry to its ``{db}.{table}`` id.

        A table id is two dot-segments (``db.table``); a ``table.column`` request
        adds a third. Keep the first two segments when there are three or more,
        otherwise return the entry unchanged.

        Args:
            entry: A judge-reported missing identifier.

        Returns:
            The reduced ``{db}.{table}`` id, or ``""`` for a blank entry.
        """
        cleaned = (entry or "").strip()
        if not cleaned:
            return ""
        parts = cleaned.split(".")
        if len(parts) >= 3:
            return ".".join(parts[:2])
        return cleaned

    def _resolve_missing_to_tables(self, missing: List[str]) -> List[str]:
        """Reduce judge ``missing`` entries to de-duplicated real table ids.

        With a ``chunks_lookup`` configured, an entry survives only if a non-empty
        chunk exists for its resolved table (dropping hallucinated targets). Without
        one, every non-blank resolved id is kept (the unit-test path has no chunk
        store to validate against).

        Args:
            missing: Judge-reported missing identifiers (table or ``table.column``).

        Returns:
            De-duplicated real table ids to fold into the candidate set.
        """
        out: List[str] = []
        for entry in missing or []:
            table_id = self._reduce_to_table_id(entry)
            if not table_id or table_id in out:
                continue
            if self.chunks_lookup is None:
                out.append(table_id)
                continue
            chunk = self.chunks_lookup(
                table_ids=[table_id], namespace=self._namespace,
            ) or {}
            if chunk.get(table_id):
                out.append(table_id)
            else:
                logger.info("slice.expand: dropping unfetchable missing entry %r "
                            "(resolved table %r has no chunk)", entry, table_id)
        return list(dict.fromkeys(out))

    def _slice_dict(self) -> Dict[str, Any]:
        """Assemble the slice dict, parsing chunks and injecting few-shots.

        Returns:
            ``{tables, columns, joins, acord_paths, query_patterns, few_shots}``.
            Schema-detail keys stay present-but-empty when no ``chunks_lookup`` is
            configured; ``few_shots`` is always populated from ``kb_retrieve``.
        """
        columns: List[Dict[str, str]] = []
        joins: List[Dict[str, str]] = []
        acord_paths: Dict[str, str] = {}
        patterns: List[str] = []
        # Tables default to the recalled candidates; when a chunk source is present
        # we narrow to the candidates that actually produced a fetched chunk (a
        # phantom id must not appear as a real join target).
        tables: List[str] = list(self._candidates)
        if self.chunks_lookup is not None:
            chunks = self.chunks_lookup(
                table_ids=self._candidates, namespace=self._namespace,
            ) or {}
            for tid in self._candidates:
                md = chunks.get(tid, "")
                if not md:
                    continue
                columns.extend(_parse_columns(md=md, table_id=tid))
                joins.extend(_parse_reference_joins(md=md, table_id=tid))
                acord = _parse_acord_path(md=md)
                if acord:
                    acord_paths[tid] = acord
                patterns.extend(_parse_query_patterns(md=md))
            tables = [tid for tid in self._candidates if chunks.get(tid)]
        # Inject the sample-SQL / glossary few-shots for the question — the Hybrid
        # slice's addition over the RAG slice.
        few_shots = self.kb_retrieve(self._question, self._namespace) or []
        return {
            "tables": tables,
            "columns": columns,
            "joins": joins,
            "acord_paths": acord_paths,
            "query_patterns": patterns,
            "few_shots": few_shots,
        }

    def _fit(self, payload: Dict[str, Any]) -> str:
        """Serialize ``payload`` and shrink it to fit the token budget.

        Eviction order preserves answerability: first strip verbose column
        descriptions (the bulk of the token weight, while names + types are tiny),
        then drop whole columns for the least-relevant tables (``self._candidates``
        is router-relevance order, so evict from the tail). Joins and few-shots are
        always kept — both are cheap and structurally critical for SQL generation.

        Args:
            payload: The assembled slice dict from :meth:`_slice_dict`.

        Returns:
            The serialized slice, within ``budget`` where achievable (otherwise the
            minimally-reduced text; the caller logs a degraded slice).
        """
        text = json.dumps(payload)
        if self.tokens(text) <= self.budget:
            return text
        # Step 1 — drop verbose descriptions, keep every column name / type.
        payload["columns"] = [
            {k: v for k, v in c.items() if k != "description"}
            for c in payload["columns"]
        ]
        text = json.dumps(payload)
        if self.tokens(text) <= self.budget:
            return text
        # Step 2 — evict least-relevant tables' columns first (candidates are in
        # descending relevance order, so walk the tail).
        present = [t for t in self._candidates if t in set(payload["tables"])]
        for t in reversed(present):
            payload["columns"] = [
                c for c in payload["columns"] if c.get("table_id") != t
            ]
            text = json.dumps(payload)
            if self.tokens(text) <= self.budget:
                return text
        return text  # reduced to the minimum; caller logs a degraded slice
