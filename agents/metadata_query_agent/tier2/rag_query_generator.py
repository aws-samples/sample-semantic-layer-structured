"""Phase 3 (RAG): NL→SQL with one repair round on parse error."""
from __future__ import annotations

import logging
from typing import Any, Callable

from .sql_validator import SqlSyntaxError, validate_sql

logger = logging.getLogger(__name__)

# Join-path guidance for the generation prompt. The slice's ``joins`` block lists
# the authoritative connection edges between tables (each from the source table's
# declared reference joins). The model MUST connect tables using only those edges
# — and when two tables it needs do not join directly, bridge through an
# intermediate table that joins to both — rather than inventing a join predicate.
# This is the fix for the live-run failure where "market value of holdings by
# party" got a fabricated financial_activity join even though the slice carried
# the correct holding↔coverage↔party edges.
JOIN_PATH_GUIDANCE = (
    "# Join paths\n"
    "The slice's `joins` array is the AUTHORITATIVE set of connection edges "
    "between tables (each `{from, to, from_col, to_col, sql}` came from a table's "
    "declared reference joins). To connect any two tables, use ONLY these edges. "
    "If the two tables you need do not have a direct edge, find an intermediate "
    "(bridge) table in the slice that has an edge to BOTH and join THROUGH it — "
    "e.g. `holding` and `party` do not join directly; bridge through `coverage` "
    "(`holding.holding_id = coverage.holding_id`, then `coverage.party_id = "
    "party.party_id`). NEVER invent a join predicate, a `... OR <col> IS NOT NULL` "
    "catch-all, or a join on a column not named in a slice `joins` edge — a "
    "fabricated join produces a Cartesian/near-Cartesian product and wrong totals. "
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
            "- `code` — a surrogate code/id/key, typically UNIQUE per row (e.g. "
            "party_type_code). Use it for joins and exact-id filters, NOT for "
            "grouping or as a human-readable value.\n"
            "- `label` — the human-readable form of a coded value (e.g. "
            "party_type). When the question asks for 'types', 'categories', "
            "'kinds', 'most common', 'distribution of', or a 'human-readable "
            "description/label/name', GROUP BY and SELECT the `label` column, not "
            "its `code` sibling. Grouping by a `code` that is unique per row makes "
            "every count 1 — almost always wrong.\n"
            "- `generic` — an ordinary value column.\n\n"
            "# Lookup / description joins\n"
            "PREFER A LABEL COLUMN; JOIN A LOOKUP ONLY WHEN NONE EXISTS. When the "
            "question asks for the 'human-readable description', 'meaning', "
            "'label', 'type', or 'name' of a coded value, FIRST check the slice "
            "for a `label`-role column (or an inherently-readable sibling, e.g. "
            "`party_type` for `party_type_code`). If one is present, SELECT/GROUP "
            "BY that column DIRECTLY and do NOT join any code-lookup table — an "
            "INNER JOIN to a lookup whose `code_value`s do not line up with the "
            "entity's `<x>_code` silently drops EVERY row, producing a wrong "
            "0-row answer. ONLY when NO such readable column exists in the slice "
            "should you JOIN a code-lookup table (e.g. `type_codes` with "
            "`code_type`/`code_value`/`code_description`) via the slice `joins` "
            "edge and SELECT its description; prefer a LEFT JOIN so unmatched "
            "codes never erase rows. Never GROUP BY a bare surrogate code that is "
            "unique per row (every count collapses to 1); group on the "
            "label/description, keeping the code only if it is also selected.\n\n"
            "# Aggregating an entity's measure\n"
            "To aggregate (SUM/AVG/COUNT) a measure OF an entity, PREFER THAT "
            "entity's own measure column reached via the slice `joins` path — "
            "e.g. the market value of holdings is `holding.market_value`, NOT an "
            "amount column on an unrelated activity/transaction table. When the "
            "entity HAS its own measure column, do not substitute a "
            "similarly-named amount column from a different table.\n"
            "EXCEPTION — legitimate substitution from a related table: some "
            "measures have NO column on the obviously-named table because that "
            "table is empty or carries only keys (e.g. `holding_payout` and "
            "`holding_projection` hold no payout-amount/frequency column). When "
            "the question's measure is inherently an EVENT/TRANSACTION value (a "
            "payout, withdrawal, dividend, claim) and no entity column carries "
            "it, source it from the related transaction/detail table reached over "
            "a present join — e.g. a policy's payout amount from "
            "`financial_activity.transaction_amount` filtered to the relevant "
            "`activity_type` (Withdrawal/Dividend/Claim), the payout-bearing "
            "policies identified via `annuity_detail`, and the frequency from "
            "`policy_product.premium_mode` or `coverage.product_code`. This is a "
            "VALID substitution, not a fabricated column, BECAUSE the source "
            "column genuinely exists on a table in the slice and is reached via a "
            "real join path; LEFT JOIN it so a policy with no matching activity "
            "still returns a row. Only when NEITHER the entity's own measure "
            "column NOR such a real related-table source exists in the slice is "
            "the question unanswerable — never invent a column that is absent "
            "from every table.\n\n"
            "# Group/return the CANONICAL entity, not a bridge's raw FK\n"
            "When the question groups or returns results BY a named entity (e.g. "
            "'by party', 'per customer', 'for each holder'), JOIN to that "
            "entity's own table and GROUP BY / SELECT its primary key (and name), "
            "NOT the raw foreign-key column on a bridge table. A bridge FK like "
            "`coverage.party_id` holds the UNPREFIXED id and may include parties "
            "not present in the entity table, so grouping on it gives a different "
            "(usually larger) result set than the real entities. Join through to "
            "`party` (via the id-prefix transform) and GROUP BY `party.party_id` "
            "so only real, canonical entities are returned.\n\n"
            "# Optional / supplementary tables → LEFT JOIN\n"
            "Use INNER JOIN only for tables REQUIRED to answer the question. For a "
            "table that merely ENRICHES the answer (an investment-product name, a "
            "lookup label, a sub-account/detail row that may be sparse or absent), "
            "use LEFT JOIN — an INNER JOIN to a table with few/no matching rows "
            "(e.g. `holding_subaccount` has only a handful of rows) silently drops "
            "EVERY result row, producing a wrong 0-row answer. If the question's "
            "core entities are present, never let an optional enrichment join "
            "reduce the result to zero: LEFT JOIN it and COALESCE its display "
            "value (e.g. ARRAY_JOIN(ARRAY_AGG(...)) over a LEFT-joined name, or "
            "'N/A' when null). When a requested descriptive value (e.g. a product "
            "or fund NAME) can come from MORE THAN ONE related table, LEFT JOIN "
            "each available source and COALESCE across them (CASE/COALESCE: prefer "
            "the first non-null, fall back to the next, then 'N/A') rather than "
            "relying on a single sparse table — e.g. an investment/product name "
            "may live on a sub-account row when present OR on the product table "
            "reached via the entity's product_code; try both before returning "
            "'N/A'.\n\n"
            "# Join-key fidelity (id prefixes)\n"
            "Honor a join edge's `sql` / `from_col` / `to_col` exactly as the "
            "slice declares it. CRITICAL: id columns in this dataset are often "
            "stored with a TYPE PREFIX on one side but not the other (e.g. "
            "`party.party_id` = `PARTY#PARTY000042` while `coverage.party_id` = "
            "`PARTY000042`). When the two key columns hold the same logical id in "
            "different surface forms, equate them with the matching transform — "
            "`CONCAT('PARTY#', c.party_id) = p.party_id` — NOT a bare "
            "`c.party_id = p.party_id`, which silently matches zero rows. Inspect "
            "the slice's example values / descriptions for the `#` prefix pattern "
            "before writing an id equality.\n\n"
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
