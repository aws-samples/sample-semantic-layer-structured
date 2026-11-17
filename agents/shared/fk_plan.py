"""FK-plan builder + neighbor-closure, shared by the ontology agent (Phase 2)
and the REST-API delta planner (blast-radius expansion)."""

from typing import Dict, List, Set


def build_fk_plan(fragments: Dict) -> Dict[str, List[Dict[str, str]]]:
    """Parse Phase-1 fk_hints into {table: [{fk_column, target_table}]}.

    Args:
        fragments: A dict with a "tables" list; each entry has "table_name" and a
            "fk_hints" string like "col→target, col2→target2" (arrow = U+2192).

    Returns:
        Mapping of table_name to a list of {fk_column, target_table} dicts. Tables
        with no hints are absent from the mapping.
    """
    fk_plan: Dict[str, List[Dict[str, str]]] = {}
    for entry in fragments.get("tables", []):
        hints_str = entry.get("fk_hints", "")
        if not hints_str:
            continue
        for hint in hints_str.split(","):
            if "→" in hint:
                col, target = hint.split("→", 1)
                fk_plan.setdefault(entry["table_name"], []).append(
                    {"fk_column": col.strip(), "target_table": target.strip()}
                )
    return fk_plan


def fk_closure(changed: Set[str], fk_plan: Dict[str, List[Dict[str, str]]]) -> Set[str]:
    """Expand a changed-table set with inbound + outbound FK neighbors.

    Outbound: a changed table's FK targets. Inbound: any table whose FK points at
    a changed table (its edge may now be stale). Single hop is sufficient because
    a stale neighbor is itself rebuilt and re-fingerprinted this run.

    Args:
        changed: Set of bare table names that directly changed.
        fk_plan: The mapping produced by build_fk_plan.

    Returns:
        The closure set: changed plus all inbound/outbound single-hop FK neighbors.
    """
    result = set(changed)
    for table, edges in fk_plan.items():
        targets = {e["target_table"] for e in edges}
        if table in changed:
            result |= targets  # outbound
        if targets & changed:
            result.add(table)  # inbound
    return result
