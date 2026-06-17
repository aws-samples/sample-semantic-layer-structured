"""Pure-function parsers for the markdown documents the metadata_agent
writes to S3 (one per table) and Bedrock KB ingests as chunks.

Document structure (see ``agents/metadata_agent/prompt_builder.py``):

  # {catalog_id}.{database_name}.{table_name}
  ## Overview
  ## Business Purpose
  ## ACORD Source Path
  ## Reference Tables           <- one ``- `target`: JOIN ... ON ...`` per line
  ## Common Query Patterns      <- 1-3 bullet items
  ## Columns                    <- markdown table | Column | Type | Description |
  ## Sample Data
  ## Notes

These parsers are best-effort: missing sections return empty lists / None
rather than raising, because the RAG slice builder must keep working even
when a doc was authored before the format stabilized.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# Suffixes that mark a column as a surrogate code / id / key rather than a
# human-readable value. ``cd`` is the ACORD-style abbreviation for "code".
_CODE_SUFFIXES = ("_code", "_cd", "_id", "_sk", "_key", "_num")


def classify_column_role(*, name: str, sibling_names: set) -> str:
    """Classify a column as ``code`` / ``label`` / ``generic`` for the generator.

    The SQL generator uses this to pick the human-readable column when a question
    asks for "types", "categories", "most common", or "descriptions" — instead of
    a surrogate code that is unique per row (the ``party_type_code`` vs
    ``party_type`` trap). The grounding gate cannot catch that mistake because both
    columns are real.

    Heuristic (deterministic, no data scan):
      * ``code`` — the name ends in a surrogate suffix (``_code``/``_cd``/``_id``/
        ``_sk``/``_key``/``_num``).
      * ``label`` — the name is the BARE form of a coded sibling present in the
        SAME table (e.g. ``party_type`` alongside ``party_type_code``); that bare
        form is the human-readable description of the code.
      * ``generic`` — anything else.

    Args:
        name: The column name (case-insensitive).
        sibling_names: All column names in the SAME table (lower-cased), used to
            detect a bare-form/coded pair. Pass an empty set to skip the pairing
            check (then a non-coded column is always ``generic``).

    Returns:
        One of ``"code"``, ``"label"``, ``"generic"``.
    """
    n = (name or "").strip().lower()
    if not n:
        return "generic"
    if n.endswith(_CODE_SUFFIXES):
        return "code"
    # Bare form of a coded sibling in the same table → human-readable label.
    siblings = {s.strip().lower() for s in (sibling_names or set())}
    for suffix in _CODE_SUFFIXES:
        if f"{n}{suffix}" in siblings:
            return "label"
    return "generic"


def annotate_semantic_roles(columns: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Add a ``semantic_role`` key to each column dict (additive, per-table).

    Pairing (bare-form ↔ coded sibling) is scoped to the column's own table, so a
    bare ``type`` in one table is not labelled by a ``type_code`` in another.

    Args:
        columns: ``[{table_id, name, type, description}, ...]`` from
            :func:`parse_columns`.

    Returns:
        The same list, each dict gaining ``semantic_role`` (input is not mutated;
        new dicts are returned).
    """
    # Pre-compute the sibling-name set per table_id so classification is O(n).
    names_by_table: Dict[str, set] = {}
    for col in columns:
        names_by_table.setdefault(col.get("table_id", ""), set()).add(
            (col.get("name") or "").strip().lower())
    annotated: List[Dict[str, str]] = []
    for col in columns:
        siblings = names_by_table.get(col.get("table_id", ""), set())
        role = classify_column_role(name=col.get("name", ""), sibling_names=siblings)
        annotated.append({**col, "semantic_role": role})
    return annotated


def _section(*, md: str, title: str) -> Optional[str]:
    """Return the body of ``## {title}`` or ``None`` when not present.

    Matching is case-insensitive on the title and ignores leading/trailing
    whitespace; the body is everything between this heading and the next
    ``##`` (or end of document).
    """
    target = title.strip().lower()
    matches = list(_SECTION_RE.finditer(md))
    for idx, m in enumerate(matches):
        if m.group(1).strip().lower() == target:
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
            return md[start:end].strip()
    return None


def parse_columns(*, md: str, table_id: str) -> List[Dict[str, str]]:
    """Parse the ``## Columns`` markdown table.

    Returns a list of ``{table_id, name, type, description}`` dicts. Header
    and separator rows are skipped; malformed rows are ignored.
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
        # Header row has the literal "Column" / "Type" / "Description" labels.
        if name.lower() == "column" and ctype.lower() == "type":
            continue
        if not name:
            continue
        rows.append({
            "table_id": table_id, "name": name, "type": ctype, "description": desc,
        })
    return rows


def parse_reference_joins(*, md: str, table_id: str) -> List[Dict[str, str]]:
    """Parse the ``## Reference Tables`` section into structured join edges.

    Each bullet is expected in the form::

        - `target_table`: JOIN target_table x ON t.col = x.col

    Returns ``[{from, to, from_col, to_col, sql}]``. Lines without a join
    on-clause produce a row with empty ``from_col``/``to_col``.
    """
    body = _section(md=md, title="Reference Tables")
    if not body:
        return []
    edges: List[Dict[str, str]] = []
    bullet_re = re.compile(r"^-\s*`?([^`:\s]+)`?\s*:\s*(.+)$")
    on_re = re.compile(
        r"\bON\b\s+(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)", re.IGNORECASE,
    )
    for raw in body.splitlines():
        line = raw.strip()
        m = bullet_re.match(line)
        if not m:
            continue
        target, sql = m.group(1).strip(), m.group(2).strip()
        edge: Dict[str, str] = {
            "from": table_id, "to": target, "sql": sql,
            "from_col": "", "to_col": "",
        }
        on_match = on_re.search(sql)
        if on_match:
            # Heuristic: the alias on either side of ON is opaque, so we
            # report column names directly. Same column on both sides is
            # the dominant pattern in the metadata_agent prompt.
            edge["from_col"] = on_match.group(2)
            edge["to_col"] = on_match.group(4)
        edges.append(edge)
    return edges


def parse_acord_path(*, md: str) -> Optional[str]:
    """Return the ACORD path string from ``## ACORD Source Path`` or None."""
    body = _section(md=md, title="ACORD Source Path")
    if not body:
        return None
    # Body is a single non-empty line in the standard template.
    for line in body.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def parse_query_patterns(*, md: str) -> List[str]:
    """Return the list of bullet entries under ``## Common Query Patterns``."""
    body = _section(md=md, title="Common Query Patterns")
    if not body:
        return []
    out: List[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            out.append(line[2:].strip())
        elif line.startswith("* "):
            out.append(line[2:].strip())
    return out
