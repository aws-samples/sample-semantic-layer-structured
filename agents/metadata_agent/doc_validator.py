"""Source-side schema validation for the markdown documents the metadata agent
writes to S3 (one per table) and Bedrock KB ingests as chunks.

The metadata agent fetches a table's real schema via ``get_single_table_schema``
but the LLM then composes the ``## Columns`` and ``## Reference Tables`` sections
freely. Without a check, a hallucinated column (e.g. ``holding.party_id`` when the
real ``holding`` only has ``policy_id``) or a fabricated join edge (e.g.
``holding_subaccount.invest_product_id`` when that column does not exist) is
written verbatim to the KB. The query agent's Phase-3 slice builder then copies
those fabrications faithfully, and the SQL generator emits ungroundable SQL.

These pure functions parse the same sections the query agent's
``tier2/markdown_slice_parser.py`` reads (kept behaviourally identical so
validation matches how the doc is consumed downstream) and DROP any column row /
join edge that references a column absent from the authoritative table schema.

The comparison is case-insensitive: Glue/Iceberg return lower-cased column names
but a doc may use mixed case (e.g. ``MarketValue``), and a false drop of a REAL
column would be worse than the bug we are fixing.

No AWS calls live here — the caller supplies the real column sets. This keeps the
module fully unit-testable and free of cross-package coupling (it deliberately
does NOT import the query agent's parser, which is not packaged into the metadata
container).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

# Heading matcher: ``## Columns`` etc. Mirrors markdown_slice_parser._SECTION_RE.
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# ``- `target_table`: JOIN target_table x ON t.col = x.col``
_BULLET_RE = re.compile(r"^-\s*`?([^`:\s]+)`?\s*:\s*(.+)$")
_ON_RE = re.compile(r"\bON\b\s+(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)", re.IGNORECASE)


def _section_bounds(*, md: str, title: str) -> Optional[Tuple[int, int]]:
    """Return the ``(start, end)`` character offsets of the body of ``## {title}``.

    ``start`` is the offset just after the heading line; ``end`` is the offset of
    the next ``##`` heading (or end of document). Matching is case-insensitive on
    the title. Returns ``None`` when the section is absent.

    Args:
        md: The full markdown document.
        title: The section title to locate (e.g. ``"Columns"``).

    Returns:
        A ``(start, end)`` tuple of character offsets, or ``None``.
    """
    target = title.strip().lower()
    matches = list(_SECTION_RE.finditer(md))
    for idx, m in enumerate(matches):
        if m.group(1).strip().lower() == target:
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
            return (start, end)
    return None


def extract_column_rows(md: str) -> List[Dict[str, str]]:
    """Parse the ``## Columns`` markdown table into row dicts.

    Mirrors ``markdown_slice_parser.parse_columns`` but additionally carries the
    verbatim ``raw_line`` so the rewriter can delete the exact source line.

    Args:
        md: The full markdown document.

    Returns:
        A list of ``{"name", "type", "description", "raw_line"}`` dicts. Header
        and separator rows are skipped; malformed rows are ignored.
    """
    bounds = _section_bounds(md=md, title="Columns")
    if bounds is None:
        return []
    body = md[bounds[0]:bounds[1]]
    rows: List[Dict[str, str]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        # Skip the markdown header divider row "|---|---|---|".
        if set(line.replace("|", "").strip()) <= {"-", " ", ":"}:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name, ctype, desc = cells[0], cells[1], cells[2]
        # Header row carries the literal "Column" / "Type" labels.
        if name.lower() == "column" and ctype.lower() == "type":
            continue
        if not name:
            continue
        rows.append({
            "name": name, "type": ctype, "description": desc, "raw_line": raw,
        })
    return rows


def extract_reference_edges(md: str) -> List[Dict[str, str]]:
    """Parse the ``## Reference Tables`` section into structured join-edge dicts.

    Mirrors ``markdown_slice_parser.parse_reference_joins`` but carries the
    verbatim ``raw_line`` so the rewriter can delete the exact source line.

    Args:
        md: The full markdown document.

    Returns:
        A list of ``{"to", "from_col", "to_col", "sql", "raw_line"}`` dicts. Edges
        without a parseable ``ON`` clause have empty ``from_col``/``to_col``.
    """
    bounds = _section_bounds(md=md, title="Reference Tables")
    if bounds is None:
        return []
    body = md[bounds[0]:bounds[1]]
    edges: List[Dict[str, str]] = []
    for raw in body.splitlines():
        line = raw.strip()
        m = _BULLET_RE.match(line)
        if not m:
            continue
        target, sql = m.group(1).strip(), m.group(2).strip()
        edge: Dict[str, str] = {
            "to": target, "sql": sql, "from_col": "", "to_col": "",
            "raw_line": raw,
        }
        on_match = _ON_RE.search(sql)
        if on_match:
            # The two aliases are opaque, so report bare column names. The
            # metadata_agent prompt's dominant pattern is same-id-both-sides.
            edge["from_col"] = on_match.group(2)
            edge["to_col"] = on_match.group(4)
        edges.append(edge)
    return edges


def validate_and_clean(
    *,
    md: str,
    real_columns: Set[str],
    target_columns: Optional[Dict[str, Set[str]]] = None,
    layer_tables: Optional[Set[str]] = None,
) -> Tuple[str, List[str]]:
    """Drop hallucinated column rows and join edges from a metadata document.

    A column row is dropped when its column name is absent from the table's real
    schema. A join edge is dropped when:
      * its ``from_col`` (the column on THIS table) is absent from the real schema;
      * its ``to_col`` is absent from a KNOWN target table's columns; or
      * its target table is absent from ``layer_tables`` — i.e. the edge points at
        a table that was NOT materialized in this semantic layer (e.g. an ACORD
        ``participant`` / ``payout`` table the ontology describes but the layer
        never built). Such an out-of-layer reference is the highest-confidence
        drop: the query agent searches for the named table, fails, and wrongly
        reports the data as missing. ``to_col`` validation stays best-effort (the
        edge is kept when the target schema is merely unresolvable), but an
        out-of-LAYER target is dropped outright.

    All comparisons are case-insensitive to avoid false-dropping a real column
    written in a different case than the catalog returns.

    Args:
        md: The full markdown document the agent is about to save.
        real_columns: Lower-cased column names that actually exist on THIS table.
            An empty set disables column/from_col validation (caller could not
            resolve the schema — never block the save on an infra failure).
        target_columns: Optional map of ``{target_table_lower: {col_lower, ...}}``
            for tables named in ``## Reference Tables``. Tables absent from this
            map have their ``to_col`` left unvalidated.
        layer_tables: Optional set of bare table names that exist in THIS semantic
            layer. When provided, a ## Reference Tables edge whose target is not in
            this set is dropped (an out-of-layer / hallucinated table). When None
            or empty, target-table membership is not checked (back-compat).

    Returns:
        A ``(cleaned_md, dropped_identifiers)`` tuple. ``dropped_identifiers`` are
        human-readable, e.g. ``"column:party_id"`` /
        ``"join:holding_subaccount.invest_product_id"`` /
        ``"join:participant(not-in-layer)"``. When nothing is dropped the original
        ``md`` is returned unchanged.
    """
    # No authoritative schema → cannot validate columns; skip rather than block.
    # (The layer-membership check below still applies — it does not depend on
    # THIS table's columns — but column/from_col/to_col checks all need them, and
    # without a schema we conservatively skip the whole pass as before.)
    if not real_columns:
        return md, []

    targets = {
        t.lower(): {c.lower() for c in cols}
        for t, cols in (target_columns or {}).items()
    }
    real_lower = {c.lower() for c in real_columns}
    layer_lower = {t.lower() for t in (layer_tables or set())}

    lines_to_drop: Set[str] = set()
    dropped: List[str] = []

    # --- Columns: drop rows whose name is not a real column -------------------
    for row in extract_column_rows(md):
        name = row["name"]
        if name.lower() not in real_lower:
            lines_to_drop.add(row["raw_line"])
            dropped.append(f"column:{name}")

    # --- Reference Tables: drop edges referencing a non-existent column -------
    for edge in extract_reference_edges(md):
        from_col = (edge.get("from_col") or "").lower()
        to_col = (edge.get("to_col") or "").lower()
        target = (edge.get("to") or "").lower()

        bad = False
        out_of_layer = False
        # Target table not materialized in this layer → drop outright (highest
        # confidence: the downstream query agent can never resolve it).
        if layer_lower and target and target not in layer_lower:
            bad = True
            out_of_layer = True
        # from_col lives on THIS table — always checkable.
        if from_col and from_col not in real_lower:
            bad = True
        # to_col only checkable when the target table's schema was resolved.
        if to_col and target in targets and to_col not in targets[target]:
            bad = True

        if bad:
            lines_to_drop.add(edge["raw_line"])
            if out_of_layer:
                dropped.append(f"join:{edge.get('to')}(not-in-layer)")
            else:
                # Identify by the offending column on the target side when known,
                # else by this table's bad from_col.
                ident = f"{edge.get('to')}.{edge.get('to_col') or edge.get('from_col')}"
                dropped.append(f"join:{ident}")

    if not lines_to_drop:
        return md, []

    # Rewrite: drop the exact offending source lines. Splitting on "\n" and
    # re-joining preserves the document's other content verbatim.
    kept = [ln for ln in md.split("\n") if ln not in lines_to_drop]
    return "\n".join(kept), dropped
