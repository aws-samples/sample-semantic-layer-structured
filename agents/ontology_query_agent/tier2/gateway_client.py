"""Programmatic client for the AgentCore Neptune Gateway MCP tools.

The Tier 2 VKG graph workflow reaches Neptune **only** through the AgentCore
Gateway (no direct SigV4 / ``NEPTUNE_ENDPOINT``). The deterministic phases call
the gateway tools directly — not via an LLM tool loop — so this wraps a Strands
:class:`MCPClient` with three typed helpers:

  * :meth:`fetch_ontology` → ``get_ontology_from_neptune`` (Phase 1 candidates +
    catalog/db routing; returns the parsed ontology JSON).
  * :meth:`construct` → ``execute_sparql_query`` with ``query_type="CONSTRUCT"``
    (Phase 3 slice; returns Turtle text — see the gateway's CONSTRUCT branch).
  * :meth:`run_select` → ``execute_sparql_query`` SELECT (Phase 5 execution;
    returns ``{columns, rows}``).

Session lifecycle: the **caller** opens the MCP session (``with mcp_client:``)
around the workflow run; these helpers assume an active session and fail loudly
otherwise. Gateway tool names are namespaced ``<target>___<tool>``; the client
resolves the real name once via ``list_tools_sync`` and matches by suffix.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _result_text(result: Any) -> str:
    """Concatenate the text blocks of an ``MCPToolResult`` into one string.

    The gateway lambda returns its JSON payload as a single text content block;
    we join defensively in case the transport splits it.

    Args:
        result: The ``MCPToolResult`` returned by ``call_tool_sync``.

    Raises:
        RuntimeError: When the tool reported an error status.
    """
    def _get(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    status = _get(result, "status")
    content = _get(result, "content")
    parts: List[str] = []
    for block in content or []:
        text = _get(block, "text")
        if isinstance(text, str):
            parts.append(text)
    joined = "".join(parts)
    if status == "error":
        raise RuntimeError(f"gateway tool error: {joined[:500]}")
    # Some gateway transports surface the lambda's {statusCode, body} envelope
    # instead of unwrapping it — peel the body so callers see the inner payload.
    stripped = joined.strip()
    if stripped.startswith("{") and '"statusCode"' in stripped and '"body"' in stripped:
        try:
            env = json.loads(stripped)
            body = env.get("body")
            if isinstance(body, str):
                logger.debug("gateway result was a {statusCode,body} envelope — unwrapped body")
                return body
        except (json.JSONDecodeError, TypeError):
            pass
    # Fall back to structuredContent when no text blocks were present.
    if not joined:
        sc = _get(result, "structuredContent")
        if sc is not None:
            return json.dumps(sc)
    return joined


class NeptuneGatewayClient:
    """Typed wrapper over the Neptune Gateway MCP tools used by Tier 2."""

    def __init__(self, *, mcp_client: Any) -> None:
        """Construct the client.

        Args:
            mcp_client: A Strands ``MCPClient`` configured for the Neptune
                Gateway. Its session must be active (opened via ``with``) before
                any helper is called.
        """
        self._mcp = mcp_client
        self._tool_names: Optional[Dict[str, str]] = None

    def _resolve(self, suffix: str) -> str:
        """Return the namespaced gateway tool name ending in ``suffix``.

        Gateway targets expose tools as ``<target>___<tool>``; we list once and
        cache the suffix→full-name map. Falls back to ``suffix`` itself when no
        namespaced match is found (some transports pass the bare name).

        Args:
            suffix: The bare tool name (e.g. ``"execute_sparql_query"``).
        """
        if self._tool_names is None:
            self._tool_names = {}
            specs = self._mcp.list_tools_sync()
            for spec in specs:
                # spec is a Strands tool wrapper; its name is the namespaced id.
                name = getattr(spec, "tool_name", None) or getattr(spec, "name", None)
                if not name:
                    continue
                bare = name.split("___", 1)[1] if "___" in name else name
                self._tool_names[bare] = name
            logger.info("gateway tools resolved: %s", sorted(self._tool_names.keys()))
        resolved = self._tool_names.get(suffix, suffix)
        if suffix not in self._tool_names:
            logger.warning("gateway tool %r not in resolved set %s — using bare name",
                           suffix, sorted(self._tool_names.keys()))
        return resolved

    def _call(self, suffix: str, arguments: Dict[str, Any]) -> str:
        """Call gateway tool ``suffix`` with ``arguments``; return its text body."""
        name = self._resolve(suffix)
        result = self._mcp.call_tool_sync(
            tool_use_id=f"vkg-{suffix}-{uuid.uuid4().hex[:8]}",
            name=name,
            arguments=arguments,
        )
        return _result_text(result)

    def fetch_ontology(self, *, ontology_id: str) -> Dict[str, Any]:
        """Return the parsed ontology JSON for ``ontology_id`` (Phase 1).

        Shape: ``{classes:{iri:{label,comment}}, properties:{iri:{type,label,
        comment}}, mappings:{iri:{table,column}}, databases:[{name,catalog,
        dataSource}]}`` — or ``{"error": ...}`` when the graph is empty/missing.
        """
        raw = self._call("get_ontology_from_neptune", {"ontology_id": ontology_id})
        try:
            out = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"get_ontology_from_neptune returned non-JSON: {exc}") from exc
        logger.info("fetch_ontology(%s): classes=%d properties=%d error=%s",
                    ontology_id, len(out.get("classes", {})),
                    len(out.get("properties", {})), out.get("error"))
        return out

    def construct(self, *, sparql: str) -> str:
        """Run a SPARQL CONSTRUCT via the gateway; return Turtle text (Phase 3).

        Relies on the gateway's ``execute_sparql_query`` CONSTRUCT branch, which
        returns ``{"turtle": <text>}``.
        """
        raw = self._call("execute_sparql_query",
                         {"sparql_query": sparql, "query_type": "CONSTRUCT"})
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            # Some gateways may return raw Turtle directly — accept that too.
            return raw or ""
        if "error" in payload:
            raise RuntimeError(f"CONSTRUCT failed: {payload['error']}")
        return payload.get("turtle", "")

    def translate_sql(self, *, sparql: str, ontology_json: Dict[str, Any],
                      ontology_id: str = "") -> Dict[str, Any]:
        """Translate a SPARQL query to Athena SQL via the Ontop gateway tool.

        Phase 5 no longer runs the generated SPARQL against the schema-only
        Neptune graph (it has no instance data). Instead it asks the gateway's
        ``translate_sparql_to_sql`` tool (Ontop reformulation) to rewrite the
        grounded SPARQL into SQL against the mapped relational tables, then
        executes that SQL on Athena (where the real data lives).

        Args:
            sparql: The grounded SPARQL SELECT to translate.
            ontology_json: The ``get_ontology_from_neptune`` payload (carries the
                OBDA mappings Ontop needs to reformulate).
            ontology_id: The ontology id. When non-empty it is sent as
                ``ontologyId`` so the Java Handler keys its warm reformulator
                cache (PC=1) on a stable id instead of falling back to a content
                hash. Best-effort: an empty id is omitted from the call.

        Returns:
            The parsed tool payload: ``{sql, database, catalog}`` on success, or
            ``{"error": ...}`` when the translation failed. The caller inspects
            ``error``/``sql`` rather than this method raising.
        """
        arguments: Dict[str, Any] = {"sparql": sparql, "ontologyJson": ontology_json}
        # Stable id lets the Handler reuse its warm reformulator cache (PC=1);
        # omit when empty so the Handler's content-hash fallback still applies.
        if ontology_id:
            arguments["ontologyId"] = ontology_id
        raw = self._call("translate_sparql_to_sql", arguments)
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"translate_sparql_to_sql returned non-JSON: {exc}") from exc
        logger.info("translate_sql: sql=%s database=%s catalog=%s error=%s",
                    bool(payload.get("sql")), payload.get("database"),
                    payload.get("catalog"), payload.get("error"))
        return payload

    def run_select(self, *, sparql: str) -> Dict[str, Any]:
        """Run a SPARQL SELECT via the gateway; return ``{columns, rows}`` (Phase 5).

        Flattens the SPARQL-results JSON bindings to row lists in ``head.vars``
        order (missing/optional vars → empty strings).
        """
        raw = self._call("execute_sparql_query",
                         {"sparql_query": sparql, "query_type": "SELECT"})
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"SELECT returned non-JSON: {exc}") from exc
        if "error" in payload:
            raise RuntimeError(f"SELECT failed: {payload['error']}")
        columns: List[str] = list(payload.get("head", {}).get("vars", []) or [])
        rows: List[list] = []
        for binding in payload.get("results", {}).get("bindings", []) or []:
            rows.append([binding.get(col, {}).get("value", "") for col in columns])
        return {"columns": columns, "rows": rows, "sparql_query": sparql}
