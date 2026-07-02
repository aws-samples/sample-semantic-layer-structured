"""Phase 3 (RAG): NL→SQL with one repair round on parse error."""
from __future__ import annotations

import logging
from typing import Any, Callable

from .sql_validator import SqlSyntaxError, validate_sql

logger = logging.getLogger(__name__)

# Join-path guidance for the generation prompt. LAYER-AGNOSTIC by design: it
# describes the SHAPE of a correct join (use only the slice's declared edges;
# bridge through an intermediate table when two tables don't join directly) in
# neutral terms. It must NOT name any specific layer's tables/columns — the
# authoritative edges (including the exact join predicate, with any key transform)
# live in the slice's ``joins`` array, which the model reads at run time. This is
# the fix for the live-run failure where a measure question got a fabricated join
# even though the slice carried the correct bridge edges.
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


class RagQueryGenerator:
    """Generate SQL from a slice + question, with one syntax-repair attempt."""

    def __init__(self, *, agent_factory: Callable[[], Any], dialect: str) -> None:
        """Construct the generator.

        Args:
            agent_factory: Builds a fresh Strands Agent on each attempt — keeps
                conversation state from leaking between the initial generation
                and the repair round.
            dialect: sqlglot dialect identifier used for parse validation.
        """
        self.agent_factory = agent_factory
        self.dialect = dialect
        # Accumulated token usage across all agent calls in the most recent
        # generate() (initial + any repair). Read by Phase 4 to roll into the
        # workflow's running total. Reset at the start of each generate().
        self.last_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}

    def _accumulate_usage(self, result: Any) -> None:
        """Fold one agent call's usage into ``self.last_usage`` (best-effort).

        Uses the shared extractor so cache-read/write tokens are captured too
        (Bedrock folds them into totalTokens under cache_config=auto); each
        ``generate()`` call uses a FRESH agent per ``_ask``, so summing
        per-agent accumulated_usage does not double-count.
        """
        try:
            from agents.shared.tier2_graph import extract_usage
        except ImportError:  # container path: agents/ is on PYTHONPATH
            from shared.tier2_graph import extract_usage  # type: ignore
        delta = extract_usage(result)
        for key, value in delta.items():
            self.last_usage[key] = self.last_usage.get(key, 0) + int(value or 0)

    def _ask(self, agent: Any, slice_text: str, question: str,
             repair: str = "", grounding_feedback: str = "") -> str:
        """Build the prompt and run the agent, returning the trimmed SQL text.

        Args:
            agent: A fresh Strands agent for this attempt.
            slice_text: The serialized schema slice.
            question: The natural-language question.
            repair: A sqlglot parse error from a prior attempt, if any.
            grounding_feedback: A grounding-gate hint naming identifiers the
                previous SQL referenced that are NOT in the slice — the model
                must rewrite using only slice tables/columns.
        """
        suffix = ""
        if repair:
            suffix += f"\n\nPrevious attempt had parse error: {repair}"
        if grounding_feedback:
            suffix += (
                f"\n\nIMPORTANT: your previous SQL referenced identifiers that "
                f"do NOT exist in the slice: {grounding_feedback}. Rewrite the "
                f"query using ONLY the tables and columns present in the slice "
                f"above. Do not invent type/discriminator columns; if the slice "
                f"lacks a column needed to express a filter, omit that filter or "
                f"use a column that IS in the slice."
            )
        prompt = (
            f"# Slice (JSON)\n{slice_text}\n\n"  # nosec B608 — SQL/SPARQL built from internal schema-slice/static identifiers, not user input (grounding-gated)
            f"# Question\n{question}{suffix}\n\n"
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
        result = agent(prompt)
        self._accumulate_usage(result)
        return result.message['content'][0]['text'].strip()

    def generate(self, *, slice_text: str, question: str,
                 grounding_feedback: str = "") -> str:
        """Generate SQL; on a parse error, run exactly one repair round.

        Args:
            slice_text: The serialized schema slice.
            question: The natural-language question.
            grounding_feedback: Optional hint from the Phase 5 grounding gate
                naming hallucinated identifiers from a prior round so this
                regeneration avoids them.
        """
        self.last_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        agent = self.agent_factory()
        sql = self._ask(agent, slice_text, question,
                        grounding_feedback=grounding_feedback)
        try:
            validate_sql(sql, dialect=self.dialect)
            return sql
        except SqlSyntaxError as e:
            logger.info("phase3.sql_repair attempt — error=%s", e)
            agent2 = self.agent_factory()
            repaired = self._ask(agent2, slice_text, question, repair=str(e),
                                 grounding_feedback=grounding_feedback)
            validate_sql(repaired, dialect=self.dialect)
            return repaired
