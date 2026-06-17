"""Phase 5 execution half (VKG) — bounded Strands agent that runs grounded SPARQL.

The grounding gate (``grounding.check_grounding``) runs first in the Phase 5
node; only once the SPARQL is grounded does this execution agent run. It is
scoped to **two** tools — ``execute_sparql_query`` (primary, against Neptune via
the AgentCore Gateway MCP) and ``map_sql_results_to_rdf`` (n_quads shaping for
the citations panel) — and prompted (``EXECUTION_PROMPT``) to execute the
provided SPARQL, fix Neptune errors within a tight budget, re-check zero-row
results, and respect the LIMIT contract. It must NOT re-discover the ontology.

``run_execution`` returns ``{answer, usage, n_quads}`` — the RAG fix applied to
VKG: the parsed result rows carry no prose answer, so the execution agent's text
+ token usage must be threaded back to Phase 5 (the response otherwise regressed
to a generic line + ``usage:{}``).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Row cap surfaced to the user. The execution path rewrites an unlimited query
# to LIMIT (ROW_CAP + 1) so it can detect and flag truncation.
ROW_CAP = 100


_LIMIT_RE = re.compile(r"\blimit\s+\d+\b", re.IGNORECASE)


def ensure_limit(sparql: str, *, cap: int = ROW_CAP) -> Tuple[str, bool]:
    """Return ``(sparql_with_limit, injected)``.

    If ``sparql`` has no explicit ``LIMIT``, append ``LIMIT cap+1`` so the
    executor can tell whether more than ``cap`` rows would have matched.
    ``injected`` is True when a limit was added (so the caller knows to apply
    over-limit detection on the result).

    Args:
        sparql: The grounded SPARQL.
        cap: The user-facing row cap (default 100).
    """
    if _LIMIT_RE.search(sparql):
        return sparql, False
    trimmed = sparql.rstrip().rstrip(";")
    return f"{trimmed}\nLIMIT {cap + 1}", True


def apply_over_limit(result: Dict[str, Any], *, injected: bool,
                     cap: int = ROW_CAP) -> Dict[str, Any]:
    """Trim an over-cap result to ``cap`` rows and stamp over-limit flags.

    When ``injected`` is True and the query came back with ``cap+1`` rows, the
    true result exceeds the cap: trim to ``cap`` rows, set ``over_limit=True``,
    and record ``total_row_count`` as ``> cap``. Mutates and returns ``result``.

    Args:
        result: The parsed result dict (``columns``/``rows``).
        injected: Whether ``ensure_limit`` added the LIMIT (so cap+1 is meaningful).
        cap: The user-facing row cap.
    """
    rows = result.get("rows", [])
    if injected and len(rows) > cap:
        result["rows"] = rows[:cap]
        result["over_limit"] = True
        result["total_row_count"] = f">{cap}"
    else:
        result.setdefault("over_limit", False)
        result.setdefault("total_row_count", len(rows))
    return result


def build_execution_agent(*, model_factory: Callable[[], Any],
                          tools: List[Any], system_prompt: str) -> Any:
    """Construct the tightly-scoped Strands execution agent.

    Args:
        model_factory: Zero-arg callable returning a Strands BedrockModel.
        tools: The execution tools — ``[execute_sparql_query (MCP),
            map_sql_results_to_rdf]``. Passed as a list because the SPARQL tool
            arrives via the Neptune Gateway MCP client, not a local ``@tool``.
        system_prompt: The ``EXECUTION_PROMPT``.
    """
    from strands import Agent  # local import keeps unit tests light

    return Agent(
        model=model_factory(),
        system_prompt=system_prompt,
        tools=list(tools),
    )


def run_execution(*, agent: Any, sparql: str, ontology_info: str = "{}"
                  ) -> Dict[str, Any]:
    """Run the execution agent on the grounded SPARQL; return text + usage.

    Args:
        agent: The agent from :func:`build_execution_agent`.
        sparql: The grounded (LIMIT-bearing) SPARQL.
        ontology_info: The ontology JSON passed to ``map_sql_results_to_rdf``
            so it can shape n_quads; ``"{}"`` when unavailable (mapping skipped).

    Returns:
        ``{"answer": <text>, "usage": {inputTokens, outputTokens, totalTokens}}``.
    """
    prompt = (
        "Execute this SPARQL query and report the result, then map the rows to "
        "RDF n-quads.\n\n"
        f"[sparql]\n{sparql}\n[/sparql]\n"
        f"[ontology_info]\n{ontology_info}\n[/ontology_info]\n"
    )
    result = agent(prompt)
    try:
        answer = result.message["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        answer = str(result)
    usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
    try:
        acc = result.metrics.accumulated_usage
        for key in usage:
            value = (acc.get(key) if isinstance(acc, dict)
                     else getattr(acc, key, None))
            if value is not None:
                usage[key] = int(value)
    except AttributeError:
        pass
    return {"answer": answer, "usage": usage}
