"""Model-facing prompt templates for the Hybrid semantic-layer query agent.

Copied-and-owned (per the design's copy-not-import rule) from the Metadata
(SemanticRAG) query agent's ``query_prompts.py`` and
``tier2/rag_query_generator.py``. This module is a plain collection of string
templates — no runtime logic, no Bedrock/AWS imports — so it can be imported by
the unit tests without any cloud dependency.

Two model-facing prompts live here:

* :data:`SQL_GENERATION_PROMPT_TEMPLATE` — the NL→SQL generation prompt used by
  :class:`agents.semantic_layer_query_agent.sql_generator.SqlGenerator`. Its
  ``{slice_text}`` / ``{question}`` / ``{suffix}`` placeholders are filled at
  generation time. The Hybrid REGENERATE path additionally appends an
  ontology-derived negative-constraint line (``# Do NOT use: …``) so the model
  avoids a predicate/column the grounding gate flagged.
* :data:`EXECUTION_PROMPT` — the tightly-scoped Phase-5 execution-agent system
  prompt used by :mod:`agents.semantic_layer_query_agent.execution_agent`.
"""
from __future__ import annotations

# Join-path guidance for the generation prompt. LAYER-AGNOSTIC by design: it
# describes the SHAPE of a correct join (use only the slice's declared edges;
# bridge through an intermediate table when two tables don't join directly) in
# neutral terms. It must NOT name any specific layer's tables/columns — the
# authoritative edges (including the exact join predicate, with any key transform)
# live in the slice's ``joins`` array, which the model reads at run time.
JOIN_PATH_GUIDANCE = (
    "# Join paths\n"
    "The slice's `joins` array is the AUTHORITATIVE set of connection edges "
    "between tables (each `{from, to, from_col, to_col, sql}` came from a table's "
    "declared reference joins). To connect any two tables, use ONLY these edges, "
    "and write each join predicate EXACTLY as the edge's `sql` declares it. "
    "If the two tables you need do not have a direct edge, find an intermediate "
    "(bridge) table in the slice that has an edge to BOTH and join THROUGH it "
    "(table A → bridge → table B, chaining the two edges). NEVER invent a join "
    "predicate, a `... OR <col> IS NOT NULL` catch-all, or a join on a column not "
    "named in a slice `joins` edge — a fabricated join produces a "
    "Cartesian/near-Cartesian product and wrong totals. "
    "If no path of slice edges connects the tables, the question cannot be answered "
    "from this slice; do not force an unfounded join.\n\n"
)

# The body of the NL→SQL generation prompt below the slice/question header. Copied
# verbatim from the reference RagQueryGenerator so the Hybrid generator produces
# SQL with the same semantic guardrails (label-vs-code, lookup joins, measure
# aggregation, canonical-entity grouping, LEFT-join enrichment, key-transform
# fidelity, wide-federated-table projection). LAYER-AGNOSTIC by design.
_SQL_GENERATION_GUIDANCE = (
    f"{JOIN_PATH_GUIDANCE}"
    "# Column semantics\n"
    "Each slice column carries a `semantic_role`:\n"
    "- `code` — a surrogate code/id/key, typically UNIQUE per row. Use it "
    "for joins and exact-id filters, NOT for grouping or as a "
    "human-readable value.\n"
    "- `label` — the human-readable form of a coded value. When the "
    "question asks for 'types', 'categories', 'kinds', 'most common', "
    "'distribution of', or a 'human-readable description/label/name', "
    "GROUP BY and SELECT the `label` column, not its `code` sibling. "
    "Grouping by a `code` that is unique per row makes every count 1 — "
    "almost always wrong.\n"
    "- `generic` — an ordinary value column.\n\n"
    "# Lookup / description joins\n"
    "PREFER A LABEL COLUMN; JOIN A LOOKUP ONLY WHEN NONE EXISTS. When the "
    "question asks for the 'human-readable description', 'meaning', "
    "'label', 'type', or 'name' of a coded value, FIRST check the slice "
    "for a `label`-role column (or an inherently-readable sibling of the "
    "coded column). If one is present, SELECT/GROUP BY that column "
    "DIRECTLY and do NOT join any code-lookup table — an INNER JOIN to a "
    "lookup whose code values do not line up with the entity's code "
    "column silently drops EVERY row, producing a wrong 0-row answer. ONLY "
    "when NO such readable column exists in the slice should you JOIN a "
    "code-lookup table (one carrying a code→description mapping) via the "
    "slice `joins` edge and SELECT its description; prefer a LEFT JOIN so "
    "unmatched codes never erase rows. Never GROUP BY a bare surrogate "
    "code that is unique per row (every count collapses to 1); group on "
    "the label/description, keeping the code only if it is also "
    "selected.\n\n"
    "# Aggregating an entity's measure\n"
    "To aggregate (SUM/AVG/COUNT) a measure OF an entity, PREFER THAT "
    "entity's own measure column reached via the slice `joins` path, NOT a "
    "similarly-named amount column on an unrelated table. When the entity "
    "HAS its own measure column (find it by name/description in the "
    "slice's `columns`), do not substitute one from a different table.\n"
    "EXCEPTION — legitimate substitution from a related table: a measure "
    "may have NO column on the obviously-named table because that table is "
    "empty or carries only keys. A column's or table's DESCRIPTION in the "
    "slice may state where the value actually lives (e.g. that an "
    "event/transaction amount on a related detail table, filtered to a "
    "particular type, supplies the requested measure). When the slice "
    "metadata names such a source, USE it — that is a VALID substitution, "
    "not a fabricated column, BECAUSE the source column genuinely exists "
    "on a slice table reached via a real join path; LEFT JOIN it so a row "
    "with no matching detail still returns. Only when NEITHER the entity's "
    "own measure column NOR a slice-declared related-table source exists "
    "is the question unanswerable — never invent a column absent from "
    "every table.\n\n"
    "# Group/return the CANONICAL entity, not a bridge's raw FK\n"
    "When the question groups or returns results BY a named entity (e.g. "
    "'by customer', 'per account', 'for each holder'), JOIN to that "
    "entity's own table and GROUP BY / SELECT its primary key (and name), "
    "NOT the raw foreign-key column on a bridge/junction table. A bridge "
    "FK may hold a differently-formatted id and may include values not "
    "present in the entity table, so grouping on it gives a different "
    "(usually larger) result set than the real entities. Join through to "
    "the entity's own table (honoring any key transform the join edge's "
    "`sql` declares) and GROUP BY its primary key so only real, canonical "
    "entities are returned.\n\n"
    "# Optional / supplementary tables → LEFT JOIN\n"
    "Use INNER JOIN only for tables REQUIRED to answer the question. For a "
    "table that merely ENRICHES the answer (a descriptive name, a lookup "
    "label, a detail/child row that may be sparse or absent), use LEFT "
    "JOIN — an INNER JOIN to a table with few/no matching rows silently "
    "drops EVERY result row, producing a wrong 0-row answer. If the "
    "question's core entities are present, never let an optional "
    "enrichment join reduce the result to zero: LEFT JOIN it and COALESCE "
    "its display value (e.g. ARRAY_JOIN(ARRAY_AGG(...)) over a LEFT-joined "
    "name, or 'N/A' when null). When a requested descriptive value can "
    "come from MORE THAN ONE related table (the slice carries more than "
    "one joinable source for it), LEFT JOIN each available source and "
    "COALESCE across them (prefer the first non-null, fall back to the "
    "next, then 'N/A') rather than relying on a single sparse table.\n\n"
    "# Join-key fidelity (key transforms)\n"
    "Honor a join edge's `sql` / `from_col` / `to_col` EXACTLY as the "
    "slice declares it. Two key columns may hold the SAME logical id in "
    "different surface forms (e.g. one side carries a type prefix or "
    "different casing the other lacks). When the slice's join `sql` "
    "expresses the equality with a transform (a CONCAT, SUBSTR, CAST, or "
    "similar wrapping one side), reproduce that transform verbatim — a "
    "bare `a.col = b.col` in place of the declared transform silently "
    "matches zero rows. Inspect the join edge `sql` and the columns' "
    "example values / descriptions before writing any id equality; never "
    "simplify a declared key transform away.\n\n"
    "# Wide federated tables — never SELECT * / COUNT(*)\n"
    "Some tables are served by a federated connector (the slice/table or "
    "column DESCRIPTION may note a non-standard source, e.g. a NoSQL/"
    "key-value backend) and can have HUNDREDS of columns. Over such a table "
    "a `SELECT *` or `COUNT(*)` forces the connector to project EVERY "
    "attribute and fails to execute (a projection-size limit). ALWAYS "
    "project the explicit columns the question needs — never `SELECT *`. For "
    "a row count, use `COUNT(<a non-null key column>)` (e.g. the table's id/"
    "primary-key column from the slice), never `COUNT(*)`. This is safe on "
    "every backend, so prefer it universally.\n\n"
    "Output ONLY the SQL — no markdown, no commentary."
)

# Header of the NL→SQL prompt. Kept SEPARATE from the guidance body (which
# contains literal ``{…}`` JSON braces) so callers can safely ``.format()`` this
# header alone without escaping the guidance. ``{suffix}`` is where the generator
# appends repair / grounding-feedback / negative-constraint lines. The ``# nosec``
# note mirrors the reference: the SQL is built from an internal schema slice and
# static identifiers, not raw user input.
SQL_GENERATION_HEADER_TEMPLATE = (
    "# Slice (JSON)\n{slice_text}\n\n"  # nosec B608 — SQL built from internal schema-slice/static identifiers, grounding-gated
    "# Question\n{question}{suffix}\n\n"
)

# The guidance body appended verbatim after the (formatted) header. Exposed
# separately because it holds literal JSON braces that must NOT pass through
# ``str.format``.
SQL_GENERATION_GUIDANCE = _SQL_GENERATION_GUIDANCE

# Phase 5 execution agent — runs a pre-built, slice-grounded SQL query on
# Athena. Scoped tightly: its only tool is the SQL executor, and it must NOT
# rediscover schema (the grounding gate already proved the SQL grounded).
EXECUTION_PROMPT = """
You execute a single, pre-validated Athena SQL SELECT query and report the result.

The SQL has already been generated, syntax-checked, and verified to reference
only tables/columns that exist. Your job is execution and result handling — NOT
authoring new SQL.

## Rules
- Call `execute_sql_query(sql_query, database_name, catalog_id)` with the SQL and
  identifiers provided in the user message. Execute it exactly as given.
- **Never** introduce a table or column that is not already in the provided SQL.
- On an Athena error, you may attempt **at most 2** corrections, fixing only the
  specific error reported (e.g. a quoting or function-signature issue). Do not
  rewrite the query's tables, columns, or joins.
- If two consecutive executions return the **same** `query_execution_id` (stale
  cache) or the identical error, stop immediately and tell the user:
  "I was unable to retrieve results — the query returned a repeated cached
  result. Please try again in a moment."
- Do NOT run `SHOW TABLES`, `DESCRIBE`, `SELECT * LIMIT 1`, or
  `information_schema` queries to discover schema.
- If the result has **zero rows**, report the raw result as-is: state that the
  query returned 0 rows. Do **not** assert a business-level explanation for why
  no rows were returned (e.g. do not say 'no X exists' or 'there are no cases
  where Y'). Simply state that the query returned 0 rows, and note that this may
  reflect the filter or join logic rather than a confirmed business fact. Do not
  loosen the user's intent.
- Row limit: the query already enforces `LIMIT 100`. If the result indicates more
  than 100 rows would have matched, mention that the answer is truncated to the
  first 100.
- Reply with a concise 1–2 sentence plain-English answer describing the result.
"""

# Phase 5 answer-renderer prompt. The Hybrid Phase 5 is otherwise deterministic
# (bounded Athena executor, no LLM), so it produced only a row-count stub ("The
# query returned 3 row(s).") and emitted NO model span. That cost twice over:
# (a) the user/judge never sees the actual answer even when the SQL and rows are
# correct, and (b) with no in-graph answer span the SESSION
# FinalAnswerFaithfulness / GoalSuccess judges have nothing answer-like to score.
# Measured on the 2026-08-06 nb15 run: SqlGrounded 0.93 (the SQL was right) but
# GoalSuccess 0.07, with rows like Organization=7 / Trust=5 / Individual=3 hidden
# behind "The query returned 3 row(s)." This bounded renderer closes both gaps —
# a real Bedrock call whose OUTPUT is the answer, so the Strands SDK
# auto-instruments an in-graph `chat` span the eval harvester captures. It runs
# ONLY on a successful, already-grounded execution. Mirrors the VKG agent's
# equivalent renderer (ontology_query_agent.main._ANSWER_PROMPT).
ANSWER_RENDER_PROMPT = """
You report the result of an already-executed, already-validated database query in
one or two plain-English sentences. You are NOT writing or modifying SQL — the
query already ran and its result is given to you.

## Rules
- Answer the user's question directly from the result rows. For a single scalar
  result, state the value (e.g. 'There are 15 parties.'). For a small result set,
  summarize the key rows; for a large one, give the count and the top entries.
- Use ONLY the values present in the result — never invent numbers, names, or
  rows, and do not editorialize beyond what the data shows.
- If the result is empty, say the query returned no rows; do NOT assert a
  business fact (e.g. 'there are none') — note it may reflect the filter/join
  logic rather than a confirmed business fact.
- Be concise: 1-2 sentences. The UI renders the full table separately, so do not
  reproduce every row.
"""
