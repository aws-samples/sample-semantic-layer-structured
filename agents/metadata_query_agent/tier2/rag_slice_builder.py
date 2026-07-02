"""Phase 2 (RAG): build a JSON slice from Bedrock-KB markdown chunks.

Each Phase-1 candidate corresponds to one ``{database}.{table}`` markdown
document the metadata_agent ingested into the Knowledge Base. This builder
parses those documents directly to assemble the slice (tables / columns /
joins / glossary / acord_paths / query_patterns) — there is no Glue lookup
in the slice path, so Tier 2 RAG mode has zero dependency on the Glue
catalog at query time.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from agents.metadata_query_agent.tier2.markdown_slice_parser import (
        annotate_semantic_roles,
        parse_acord_path,
        parse_columns,
        parse_query_patterns,
        parse_reference_joins,
    )
except ImportError:  # container path: agents/ is on PYTHONPATH
    from metadata_query_agent.tier2.markdown_slice_parser import (  # type: ignore
        annotate_semantic_roles,
        parse_acord_path,
        parse_columns,
        parse_query_patterns,
        parse_reference_joins,
    )


# How many top-ranked Phase-1 candidates to treat as anchor (target) tables that
# _fit() must never evict. The answer table is reliably among the top few router
# hits but not always rank 0 (a party question can rank a party-satellite first),
# so protect a small head rather than only candidates[0].
_ANCHOR_TOPN = 3


class RagSliceBuilder:
    """Build a JSON slice from KB markdown chunks within a token budget."""

    def __init__(self, *,
                 chunks_lookup: Callable[..., Dict[str, str]],
                 judge_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
                 token_counter: Callable[[str], int], budget: int) -> None:
        """Construct the builder.

        Args:
            chunks_lookup: Callable ``chunks_lookup(table_ids, namespace) ->
                {table_id: markdown_body}``. Implementations typically close
                over the Phase-1 router so they can return chunks already
                cached from the most recent KB retrieval, or issue a
                metadata-filtered re-query for tables not in the cache.
            judge_fn: Decides whether the slice answers the question.
            token_counter: Counts tokens for budget enforcement.
            budget: Maximum tokens for the serialized slice.
        """
        self.chunks_lookup = chunks_lookup
        self.judge = judge_fn
        self.tokens = token_counter
        self.budget = budget
        self._candidates: List[str] = []
        self._namespace: str = ""
        # Bridge tables discovered in build() — structurally required to join the
        # candidates, so _fit() protects their columns from budget eviction.
        self._bridges: set = set()
        # Anchor (target) tables — the top-ranked Phase-1 candidates, i.e. the
        # tables the question is most about (e.g. ``party`` for "top party types").
        # _fit() never evicts an anchor's columns nor strips its descriptions:
        # dropping the very table the question targets is what made the judge
        # false-reject an EXISTING column and degrade with phase3_max_rounds. We
        # protect the top ``_ANCHOR_TOPN`` candidates (not just #1) because the
        # answer table is reliably top-ranked but not always rank 0 — a party
        # question may rank a party-satellite table first. Captured at build()
        # before expand() widens the candidate set.
        self._anchors: set = set()
        # Accumulated judge token usage across is_sufficient() calls; Phase 3
        # rolls this into the workflow's running total.
        self.judge_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}

    def build(self, *, candidates: List[str], namespace: str) -> str:
        """Build the initial slice from Phase-1 candidate table_ids.

        Before assembling, discover any **bridge tables** that connect two
        otherwise-disconnected candidates (e.g. ``holding`` and ``party`` join
        only through ``coverage``) and fold the fetchable ones into the candidate
        set, so the slice carries the transitive join path the SQL needs. Without
        this the generator would invent a direct ``holding.party_id`` (caught by
        the grounding gate → a dead-end loop) for a question that IS answerable.
        """
        self._candidates = list(candidates)
        self._namespace = namespace
        # The top-ranked candidates are the question's anchor/target tables; protect
        # them from _fit eviction so a verbose slice can never drop the target table.
        self._anchors = set(self._candidates[:_ANCHOR_TOPN])
        bridges = self.bridge_table_candidates(
            endpoints=self._candidates, namespace=namespace,
        )
        if bridges:
            logger.info("slice: added bridge table(s) %s to connect %s",
                        bridges, self._candidates)
            self._candidates = list(dict.fromkeys(self._candidates + bridges))
        self._bridges = set(bridges)
        return self._fit(self._slice_dict())

    def bridge_table_candidates(self, *, endpoints: List[str],
                                namespace: str) -> List[str]:
        """Return fetchable tables that connect two otherwise-disconnected endpoints.

        A *bridge* is a table T (not already among ``endpoints``) such that the
        reference-join edges reachable from the endpoints place T adjacent to TWO
        different endpoints that are NOT directly joined to each other. ``coverage``
        bridges ``holding`` and ``party`` this way. Pure discovery + a chunk-fetch
        availability check; it does not mutate the builder.

        Args:
            endpoints: The current candidate table_ids.
            namespace: Semantic-layer namespace for the chunk lookup.

        Returns:
            The bridge table_ids that are both needed (connect a disconnected
            endpoint pair) AND fetchable (a chunk exists). Empty when the endpoints
            already connect, or no fetchable bridge exists.
        """
        if len(endpoints) < 2:
            return []
        endpoint_set = set(endpoints)
        # Adjacency among the endpoints themselves, from THEIR own join edges.
        own_chunks = self.chunks_lookup(
            table_ids=endpoints, namespace=namespace,
        ) or {}
        # edge_targets[ep] = set of tables ep's doc declares a join to.
        edge_targets: Dict[str, set] = {}
        for ep in endpoints:
            md = own_chunks.get(ep, "")
            targets = {e.get("to") for e in parse_reference_joins(md=md, table_id=ep)}
            edge_targets[ep] = {t for t in targets if t}
        # An endpoint pair is already connected if either names the other.
        def _directly_connected(a: str, b: str) -> bool:
            return b in edge_targets.get(a, set()) or a in edge_targets.get(b, set())

        # Candidate bridges come from TWO directions, because a join edge is only
        # declared on ONE side of the relationship in the KB docs:
        #  (1) OUTBOUND — tables an endpoint's own doc names a join to.
        #  (2) INBOUND — other tables (the rest of the Phase-1 candidate set) that
        #      may declare a join TO an endpoint. The classic case: holding↔party
        #      do not name each other, and neither names coverage; but coverage's
        #      OWN doc declares joins to BOTH holding and party. Considering only
        #      outbound edges (the old behaviour) never surfaced coverage, so a
        #      perfectly answerable "market value of holdings by party" degraded.
        # We fetch each candidate's chunk and inspect ITS edges below, so an
        # inbound bridge is caught as long as it is somewhere in the candidate set.
        candidate_pool = dict.fromkeys(
            [t for ep in endpoints for t in edge_targets.get(ep, set())]
            + [t for t in self._candidates if t not in endpoint_set]
        )
        bridges: List[str] = []
        for cand in candidate_pool:
            if cand in endpoint_set:
                continue
            cand_chunk = self.chunks_lookup(
                table_ids=[cand], namespace=namespace,
            ) or {}
            cand_md = cand_chunk.get(cand, "")
            if not cand_md:
                continue  # not fetchable → can't materialise its joins; skip
            # Tables the bridge itself joins to (so we see coverage→holding AND
            # coverage→party even when holding's doc only names coverage).
            cand_targets = {
                e.get("to") for e in parse_reference_joins(md=cand_md, table_id=cand)
            } | {ep for ep in endpoints if cand in edge_targets.get(ep, set())}
            connected_eps = [ep for ep in endpoints if ep in cand_targets]
            # Does this bridge connect a pair that isn't already directly joined?
            for i in range(len(connected_eps)):
                for j in range(i + 1, len(connected_eps)):
                    if not _directly_connected(connected_eps[i], connected_eps[j]):
                        bridges.append(cand)
                        break
                else:
                    continue
                break
        return list(dict.fromkeys(bridges))

    @staticmethod
    def _reduce_to_table_id(entry: str) -> str:
        """Reduce a judge ``missing`` entry to its ``{db}.{table}`` table id.

        The SliceSufficiency judge returns either a table id (``normalized.holding``)
        or a ``table.column`` request (``normalized.holding_payout.payout_frequency``).
        Table ids are two dot-segments (``db.table``); a column request adds a third.
        Keep the first two segments when there are three or more, otherwise return the
        entry unchanged. The result is still validated against the chunk catalog by the
        caller — the segment count alone is never trusted.

        Args:
            entry: A judge-reported missing identifier.

        Returns:
            The reduced ``{db}.{table}`` id, or ``""`` for an empty/blank entry.
        """
        cleaned = (entry or "").strip()
        if not cleaned:
            return ""
        parts = cleaned.split(".")
        if len(parts) >= 3:
            return ".".join(parts[:2])
        return cleaned

    def _resolve_missing_to_tables(self, missing: List[str]) -> List[str]:
        """Reduce judge ``missing`` entries to real, fetchable table ids.

        A ``table.column`` entry is reduced to its table (the column is validated when
        the chunk is parsed). An entry is kept only if ``chunks_lookup`` returns a
        non-empty chunk for the resolved table — so a name that resolves to no real
        table (a hallucinated column on a nonexistent table, or a dotted
        column-as-table) is dropped and can never enter ``tables[]`` as a phantom join
        target.

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
            chunk = self.chunks_lookup(
                table_ids=[table_id], namespace=self._namespace,
            ) or {}
            if chunk.get(table_id):
                out.append(table_id)
            else:
                logger.info(
                    "slice.expand: dropping unfetchable missing entry %r "
                    "(resolved table %r has no chunk)", entry, table_id)
        return list(dict.fromkeys(out))

    def expand(self, *, slice_text: str, missing: List[str]) -> str:
        """Re-build the slice including judge-requested tables — fetchable ones only.

        A ``missing`` entry is folded into the candidate set only when it resolves to a
        real, fetchable table (see :meth:`_resolve_missing_to_tables`). Entries that name
        no fetchable table — chiefly the judge's dotted ``table.column`` requests for
        columns that do not exist — are dropped, so they never pollute ``tables[]`` and
        cannot mislead the next judge round or the SQL generator into a phantom join.
        """
        new_tables = self._resolve_missing_to_tables(missing)
        if not new_tables:
            # The judge asked only for columns that resolve to no fetchable table —
            # widening cannot help. Return the slice unchanged so the round ceiling
            # (not a phantom sufficiency flip) governs the degrade.
            logger.info(
                "slice.expand: no fetchable table in missing=%s; slice unchanged",
                missing)
            return self._fit(self._slice_dict())
        self._candidates = list(dict.fromkeys(self._candidates + new_tables))
        # Re-run bridge discovery over the WIDENED candidate set. A judge that
        # adds an endpoint (e.g. holding) to a party-centric slice leaves the two
        # disconnected — they join only through a bridge (coverage) that neither
        # the initial build() nor this expand() would otherwise pull in, because
        # build() ran before holding existed and expand() only folds in the exact
        # tables the judge named. Without this, the next judge round still sees an
        # unconnectable pair and re-degrades. Fold in any newly-needed bridges and
        # protect them from budget eviction (same contract as build()).
        new_bridges = self.bridge_table_candidates(
            endpoints=self._candidates, namespace=self._namespace,
        )
        fresh = [b for b in new_bridges if b not in set(self._candidates)]
        if fresh:
            logger.info("slice.expand: added bridge table(s) %s to connect %s",
                        fresh, self._candidates)
            self._candidates = list(dict.fromkeys(self._candidates + fresh))
        self._bridges |= set(new_bridges)
        return self._fit(self._slice_dict())

    def is_sufficient(self, *, slice_text: str,
                      question: str) -> Tuple[bool, Optional[List[str]]]:
        """Ask the judge whether ``slice_text`` answers ``question``."""
        verdict = self.judge({'slice': slice_text, 'question': question})
        usage = verdict.get('usage') or {}
        # Accumulate every usage key the judge reports — including the cache
        # components Bedrock folds into totalTokens — so the running total stays
        # consistent with the in/out breakdown.
        for key, value in usage.items():
            self.judge_usage[key] = self.judge_usage.get(key, 0) + int(value or 0)
        return bool(verdict.get('sufficient')), verdict.get('missing')

    def _slice_dict(self) -> Dict[str, Any]:
        """Assemble the slice dict by parsing each candidate's markdown chunk."""
        chunks = self.chunks_lookup(
            table_ids=self._candidates, namespace=self._namespace,
        ) or {}
        columns: List[Dict[str, str]] = []
        joins: List[Dict[str, str]] = []
        acord_paths: Dict[str, str] = {}
        patterns: List[str] = []
        for tid in self._candidates:
            md = chunks.get(tid, "")
            if not md:
                continue
            columns.extend(parse_columns(md=md, table_id=tid))
            joins.extend(parse_reference_joins(md=md, table_id=tid))
            acord = parse_acord_path(md=md)
            if acord:
                acord_paths[tid] = acord
            patterns.extend(parse_query_patterns(md=md))
        # Tag each column with a semantic_role (code / label / generic) so the
        # Phase 4 generator prefers the human-readable label over a surrogate code
        # when the question asks for "types" / "most common" / "descriptions"
        # (the party_type vs party_type_code trap). Pairing is scoped per table.
        columns = annotate_semantic_roles(columns)
        # Emit only candidates that produced a fetched chunk. A candidate whose chunk
        # is missing (an unfetchable id that slipped into the set, or a dotted
        # column-as-table) contributes no columns and must NOT appear in ``tables[]`` —
        # otherwise the slice judge can hallucinate it as available and the SQL
        # generator can treat it as a real (quoted) join target. This is the
        # backstop for the expand()-time filter.
        fetched_tables = [tid for tid in self._candidates if chunks.get(tid)]
        return {
            'tables': fetched_tables,
            'columns': columns,
            'joins': joins,
            'acord_paths': acord_paths,
            'query_patterns': patterns,
        }

    def _fit(self, payload: Dict[str, Any]) -> str:
        """Serialize and shrink ``payload`` to fit the token budget.

        The previous strategy dropped whole columns lists ranked by lowest table
        *degree* — which evicted exactly the answer-bearing leaf tables first
        (e.g. ``financial_activity`` joins only to ``holding`` → degree 1 → first
        to go), leaving the judge to correctly reject a slice missing its target
        columns. This rewrites eviction to preserve answerability:

        1. **Strip column descriptions** before dropping any column, EXCEPT the
           anchor (target) table's — keep the table the question is about fully
           described so the judge never false-rejects an existing target column.
           Descriptions are the bulk of the token weight; column *names + types*
           (what the judge and generator need elsewhere) are tiny. This alone
           usually fits a 20-table slice under budget without losing a column.
        2. If still over budget, drop columns **least-relevant table first** —
           walking ``self._candidates`` in REVERSE (the router returns them in
           relevance order, most-relevant first), while never evicting a discovered
           **bridge** table (structurally required for the join path) nor the
           **anchor** (the question's target table).

        Joins are always kept (cheap and structurally critical for SQL).
        """
        text = json.dumps(payload)
        if self.tokens(text) <= self.budget:
            return text
        # Step 1 — drop verbose descriptions, keep every column name/type. The
        # anchor tables keep their descriptions (they are the question's target;
        # losing their semantics is what triggered the phase3_max_rounds false-reject).
        payload['columns'] = [
            c if c.get('table_id') in self._anchors
            else {k: v for k, v in c.items() if k != 'description'}
            for c in payload['columns']
        ]
        text = json.dumps(payload)
        if self.tokens(text) <= self.budget:
            return text
        # Step 1b — if the anchors' own descriptions are what overflow, strip them too
        # (names/types still protect them from eviction in step 2). Better a terse
        # anchor than an evicted neighbor the join needs.
        payload['columns'] = [
            {k: v for k, v in c.items() if k != 'description'}
            for c in payload['columns']
        ]
        text = json.dumps(payload)
        if self.tokens(text) <= self.budget:
            return text
        # Step 2 — evict least-relevant tables' columns first, protecting bridges AND
        # the anchors. self._candidates is router-relevance order (most relevant
        # first), so reverse it; a bridge/anchor is never dropped even if low-ranked.
        present = [t for t in self._candidates if t in set(payload['tables'])]
        protected = self._bridges | self._anchors
        evict_order = [t for t in reversed(present) if t not in protected]
        for t in evict_order:
            payload['columns'] = [
                c for c in payload['columns'] if c.get('table_id') != t
            ]
            text = json.dumps(payload)
            if self.tokens(text) <= self.budget:
                return text
        return text  # truncated to bare minimum; caller logs degraded
