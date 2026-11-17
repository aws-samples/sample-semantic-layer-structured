"""Harvest short descriptions out of a table's rich KB markdown document.

The Hybrid build writes RICH prose per table to the Knowledge Base
(``kb_builder``) and a THIN structural graph to Neptune (``vkg_builder``). That
split is deliberate, but it left the graph with no human-readable text at all:
the admin viewer renders ``rdfs:comment`` in the Description column of both the
Datatype Properties and Relationships tables (see
``frontend/src/components/OntologyEditor.jsx``), so every hybrid layer showed a
column of em-dashes even though the KB document already contained a good
one-line description for every column.

This module bridges the two WITHOUT moving the prose into the graph. It reads
only two things out of the markdown — the ``## Overview`` paragraph and the
``## Columns`` table's Description cells — and condenses each to a single capped
line. Everything else (Business Purpose, Reference Tables, Common Query
Patterns, Sample Data, Notes) stays in the KB where retrieval can rank it.

Why the cap matters, and why the rest of the sections are left alone: the query
path's structural CONSTRUCT sweeps ``?s ?p ?o`` for every candidate class and
property (``semantic_layer_query_agent/neptune_gateway.py``), so ANY predicate
added here lands in the query agent's ontology slice automatically. A one-line
column description is useful context for SQL generation; a full prose section
per class would duplicate the KB retrieval and bloat every prompt.

The markdown shape parsed here is the contract the composer is instructed to
follow — ``query_prompts.KB_DOCUMENT_FORMAT`` mandates the ``## Overview``
heading and the ``| Column | Type | Description |`` table.

Isolation note: standard library only. No import from ``agents.shared``, no
sibling-agent import, no boto3 (enforced by ``tests/unit/test_hybrid_isolation.py``).
"""

import re
from typing import Dict, List, Optional

# Cap for a harvested description, in characters. Descriptions ride along in the
# query agent's ontology slice (see module docstring), so they are trimmed to one
# useful sentence-ish line rather than left unbounded.
MAX_COLUMN_DESCRIPTION_CHARS: int = 400
# The class-level overview is ONE literal per table rather than one per column,
# so it can afford to be longer than a column description.
MAX_TABLE_DESCRIPTION_CHARS: int = 800

# A markdown ATX heading, e.g. "## Columns". Captures the level and the title.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
# A markdown table separator row, e.g. "|--------|------|-------------|".
_TABLE_SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|$")


def _split_sections(markdown: str) -> Dict[str, List[str]]:
    """Split a markdown document into its heading sections.

    Args:
        markdown: The full markdown document text.

    Returns:
        dict[str, list[str]]: Lowercased heading title -> the lines beneath it,
            up to the next heading of any level. Content before the first
            heading is dropped (it is the document's H1 title line).
    """
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in markdown.splitlines():
        heading = _HEADING_RE.match(line.strip())
        if heading:
            current = heading.group(2).strip().lower()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _flatten(text: str, *, max_chars: int) -> str:
    """Condense markdown prose to a single capped plain-text line.

    Strips the inline markup that would otherwise show up as literal ``**`` and
    backticks in the viewer's table cell, collapses all whitespace (including
    newlines, which cannot appear raw in an N-Quads literal anyway) to single
    spaces, and truncates at the last sentence boundary that fits so a cell never
    ends mid-word.

    Args:
        text: The raw markdown fragment.
        max_chars: Maximum length of the returned line.

    Returns:
        str: The condensed line, or ``""`` when ``text`` carries no prose.
    """
    plain = text.replace("**", "").replace("`", "").replace("*", "")
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) <= max_chars:
        return plain
    head = plain[:max_chars]
    # Prefer cutting after a sentence; only accept a boundary in the last third
    # so a document that opens with an abbreviation does not truncate to nothing.
    boundary = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if boundary > max_chars // 3:
        return head[: boundary + 1]
    return head.rstrip() + "…"


def _first_paragraph(lines: List[str]) -> str:
    """Return the first non-empty paragraph from a section's lines.

    Args:
        lines: The lines beneath a heading.

    Returns:
        str: The paragraph text (lines joined by spaces), or ``""`` if the
            section is empty.
    """
    paragraph: List[str] = []
    for line in lines:
        if line.strip():
            paragraph.append(line.strip())
        elif paragraph:
            break
    return " ".join(paragraph)


def _parse_columns_table(lines: List[str]) -> Dict[str, str]:
    """Parse a ``| Column | Type | Description |`` markdown table.

    Args:
        lines: The lines beneath the ``## Columns`` heading.

    Returns:
        dict[str, str]: Column name -> raw description cell. Column names are
            returned lowercased because catalogs disagree on column-name case
            (the same reason the query side lowercases its inverse index), and
            the caller looks names up against the live Glue column list. Rows
            without a description cell are omitted rather than mapped to "".
    """
    descriptions: Dict[str, str] = {}
    seen_header = False
    for line in lines:
        row = line.strip()
        if not row.startswith("|"):
            continue
        if _TABLE_SEPARATOR_RE.match(row):
            continue
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) < 3:
            continue
        if not seen_header:
            # The first data-shaped row is the header ("Column | Type | ...").
            seen_header = True
            if cells[0].lower() in ("column", "column name", "name"):
                continue
        name = cells[0].strip("`* ").lower()
        description = cells[2]
        if not name or not description:
            continue
        descriptions[name] = description
    return descriptions


def parse_kb_document(markdown: str) -> Dict[str, object]:
    """Harvest the table description and per-column descriptions from a KB doc.

    Args:
        markdown: The full KB markdown document for one table, as written by
            ``kb_builder.write_kb_document``.

    Returns:
        dict: ``{"table": str, "columns": dict[str, str]}`` — the condensed
            ``## Overview`` line (``""`` when the section is missing or empty)
            and a lowercased column-name -> condensed-description map (empty
            when the ``## Columns`` table is missing or unparseable).

    This never raises on a malformed document. The caller treats a missing
    description as "no comment to emit" and logs the gap loudly; aborting an
    otherwise-correct structural build over descriptive text would be a worse
    failure than a viewer cell showing an em-dash.
    """
    sections = _split_sections(markdown or "")
    table_description = _flatten(
        _first_paragraph(sections.get("overview", [])),
        max_chars=MAX_TABLE_DESCRIPTION_CHARS,
    )
    columns = {
        name: _flatten(raw, max_chars=MAX_COLUMN_DESCRIPTION_CHARS)
        for name, raw in _parse_columns_table(sections.get("columns", [])).items()
    }
    return {
        "table": table_description,
        "columns": {name: text for name, text in columns.items() if text},
    }
