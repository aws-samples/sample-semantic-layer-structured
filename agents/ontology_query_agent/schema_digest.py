"""
Schema digest for the query agent.

Produces a compact human-readable summary of the ontology — class names,
property names, table mappings, and rdfs:label / rdfs:comment text — so the
LLM can pick the correct column without re-reading the full Neptune payload.

The digest is injected into the agent's user input as a [schema_digest:] block.
"""

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Cap the digest so we never blow the context window on huge ontologies.
MAX_DIGEST_CHARS = 12000


def _short_uri(uri: str) -> str:
    if not uri:
        return ""
    return uri.rsplit("/", 1)[-1].split("#")[-1]


def _label_of(info: Any) -> str:
    if not isinstance(info, dict):
        return ""
    for key in ("label", "rdfs:label"):
        v = info.get(key)
        if v:
            return str(v)
    return ""


def _comment_of(info: Any) -> str:
    if not isinstance(info, dict):
        return ""
    for key in ("comment", "rdfs:comment"):
        v = info.get(key)
        if v:
            return str(v)
    return ""


def build_schema_digest(ontology: Dict[str, Any]) -> str:
    """
    Build a compact schema digest from a normalized ontology dict.

    Output shape:
        TABLE db.customers (Customer): a person who holds at least one policy
          - customer_id (CustomerId): unique identifier
          - first_name (FirstName)
        TABLE db.policy (Policy): an insurance policy
          - policy_number (PolicyNumber)
        ...
    """
    if not isinstance(ontology, dict):
        return ""

    classes = ontology.get("classes", {}) or {}
    mappings = ontology.get("mappings", {}) or {}
    properties = ontology.get("properties", {}) or {}

    # Group properties by the table they map to.
    columns_by_table: Dict[str, List[Dict[str, str]]] = {}
    for prop_uri, prop_info in properties.items():
        col_path = ""
        m = mappings.get(prop_uri, {})
        if isinstance(m, dict):
            col_path = m.get("column", "") or ""
        if not col_path or "." not in col_path:
            continue
        table_name, col_name = col_path.split(".", 1)
        columns_by_table.setdefault(table_name, []).append({
            "column": col_name,
            "label": _short_uri(prop_uri),
            "comment": _comment_of(prop_info),
        })

    lines: List[str] = []
    for class_uri, class_info in classes.items():
        m = mappings.get(class_uri, {})
        table_full = m.get("table", "") if isinstance(m, dict) else ""
        if not table_full:
            continue
        class_label = _label_of(class_info) or _short_uri(class_uri)
        comment = _comment_of(class_info)
        header = f"TABLE {table_full} ({class_label})"
        if comment:
            header += f": {comment}"
        lines.append(header)

        # Columns mapped to this table
        table_short = table_full.split(".", 1)[1] if "." in table_full else table_full
        for col in columns_by_table.get(table_short, []):
            row = f"  - {col['column']} ({col['label']})"
            if col["comment"]:
                row += f": {col['comment']}"
            lines.append(row)

    digest = "\n".join(lines)
    if len(digest) > MAX_DIGEST_CHARS:
        logger.warning(
            "Schema digest truncated from %d to %d chars",
            len(digest), MAX_DIGEST_CHARS,
        )
        digest = digest[:MAX_DIGEST_CHARS] + "\n... [truncated]"
    return digest


def build_schema_digest_from_json(ontology_json: str) -> str:
    """Convenience wrapper that accepts the raw JSON string from Neptune."""
    if not ontology_json:
        return ""
    try:
        return build_schema_digest(json.loads(ontology_json))
    except Exception as e:
        logger.warning("Failed to build schema digest: %s", e)
        return ""
