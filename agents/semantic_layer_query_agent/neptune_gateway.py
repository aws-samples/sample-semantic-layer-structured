"""Thin-VKG structural-slice fetch over the AgentCore Neptune Gateway.

Phase 5 of the Hybrid workflow verifies generated SQL against the thin VKG. It
needs only the *structural neighbourhood* of the Phase-1 candidate tables — the
classes, their datatype properties, the ``rdfs:domain``/``rdfs:range`` triples,
and the Ontop ``vkg:mapsToTable`` / ``vkg:mapsToColumn`` mappings. Pulling the
whole graph would defeat the "thin" premise, so this module builds a targeted
SPARQL CONSTRUCT and runs it through the gateway's ``execute_sparql_query`` tool
(CONSTRUCT branch, which returns ``{"turtle": ...}``).

Copy-and-own: the MCP plumbing is copied in shape from
``ontology_query_agent/tier2/gateway_client.py`` rather than imported —
``tests/unit/test_hybrid_isolation.py`` forbids this package from importing
``agents.shared`` or sibling agent packages. Module scope imports only the
standard library; ``strands`` / ``mcp_proxy_for_aws`` are imported lazily.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from .ontop_mappings import VIRTUAL_KG_VOCAB

logger = logging.getLogger(__name__)

SPARQL_TOOL = "execute_sparql_query"
ONTOLOGY_TOOL = "get_ontology_from_neptune"
TRANSLATE_TOOL = "translate_sparql_to_sql"

# The named graph the creation agent writes each hybrid layer into. Must stay in
# lockstep with semantic_layer_creation_agent.main::vkg_graph_iri.
VKG_GRAPH_PREFIX = "https://semantic-layer.aws/vkg/"


def vkg_graph_iri(*, namespace: str) -> str:
    """Return the named-graph IRI holding the thin VKG for ``namespace``.

    Parameters:
        namespace: The semantic-layer namespace / layer id.
    Returns:
        str: The named-graph IRI (no angle brackets).
    """
    return f"{VKG_GRAPH_PREFIX}{namespace}"


def escape_sparql_literal(value: str) -> str:
    """Escape a string for safe use inside a quoted SPARQL literal.

    Candidate table names come from the catalog rather than end users, but an
    unescaped quote or backslash would still produce a syntactically invalid (or
    semantically altered) query, so escape unconditionally.

    Parameters:
        value: The raw literal text.
    Returns:
        str: The text with backslash, double-quote, newline, CR and tab escaped.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def normalize_candidate(candidate: str) -> str:
    """Reduce a candidate id to the bare table name the VKG maps to.

    Phase 1 emits candidates as ``{database}.{table}`` (sometimes
    ``{catalog}.{database}.{table}``), but ``vkg:mapsToTable`` stores only the
    physical table name, so the qualifier must be stripped before matching.

    Parameters:
        candidate: The candidate id, qualified or bare.
    Returns:
        str: The lowercased bare table name.
    """
    return candidate.strip().rsplit(".", 1)[-1].strip().lower()


def build_structural_construct(*, namespace: str, candidates: List[str]) -> str:
    """Build the SPARQL CONSTRUCT for the candidates' structural neighbourhood.

    Two UNION branches, both scoped to the layer's named graph:

      * **A** — subjects that are classes whose ``vkg:mapsToTable`` literal is one
        of the candidate tables. Emits their ``rdf:type`` / ``rdfs:label`` /
        ``mapsToTable`` triples.
      * **B** — subjects whose ``rdfs:domain`` is one of those classes, i.e. every
        datatype/object property hanging off a candidate table. Emits their
        ``rdfs:range`` and ``vkg:mapsToColumn`` triples too.

    Each branch binds only the SUBJECT and ``?tableName``, so the shared
    ``?s ?p ?o`` pattern then sweeps all of that subject's triples — no predicate
    enumeration needed, and new structural predicates are picked up automatically.

    Matching is case-insensitive via ``LCASE`` because ``build_inverse_index``
    lowercases its keys and catalogs disagree on table-name case.

    SCOPING: the ``VALUES`` block and the ``FILTER`` that reads ``?table`` MUST
    live in the same group graph pattern. A SPARQL ``FILTER`` only sees variables
    bound within its own group, so a ``FILTER(... = ?table)`` placed inside a
    UNION branch cannot see a ``VALUES ?table`` in the enclosing group — ``?table``
    is unbound there and every comparison fails, silently yielding an EMPTY slice.
    The filter is therefore applied AFTER the union, in the same group as
    ``VALUES``, with each branch binding ``?tableName`` for it to test.

    Parameters:
        namespace: The semantic-layer namespace whose VKG graph to query.
        candidates: The candidate table ids (``db.table`` or bare ``table``).
    Returns:
        str: The SPARQL CONSTRUCT query text.
    Raises:
        ValueError: ``candidates`` is empty (or normalizes to nothing). An
            unfiltered CONSTRUCT would pull the entire graph, so fail loud.
    """
    tables = sorted({normalize_candidate(c) for c in candidates if c and c.strip()})
    if not tables:
        raise ValueError(
            "build_structural_construct requires at least one candidate table; "
            "an unconstrained CONSTRUCT would fetch the whole VKG graph."
        )
    values = " ".join(f'"{escape_sparql_literal(t)}"' for t in tables)
    graph = vkg_graph_iri(namespace=namespace)
    maps_to_table = f"{VIRTUAL_KG_VOCAB}mapsToTable"
    return f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
CONSTRUCT {{ ?s ?p ?o }}
WHERE {{
  GRAPH <{graph}> {{
    VALUES ?table {{ {values} }}
    {{
      # Branch A: the class that maps to a candidate table.
      ?s <{maps_to_table}> ?tableName .
    }} UNION {{
      # Branch B: every property whose domain is such a class.
      ?cls <{maps_to_table}> ?tableName .
      ?s rdfs:domain ?cls .
    }}
    # Applied here, NOT inside the branches: a FILTER cannot see ?table bound by
    # the VALUES block of an enclosing group (see SCOPING in the docstring).
    FILTER(LCASE(STR(?tableName)) = ?table)
    ?s ?p ?o .
  }}
}}"""


def _result_text(result: Any) -> str:
    """Concatenate an MCP tool result's text blocks, peeling the Lambda envelope.

    Parameters:
        result: The ``MCPToolResult`` from ``call_tool_sync`` (dict- or
            attribute-shaped).
    Returns:
        str: The tool's payload text.
    Raises:
        RuntimeError: The tool reported ``status == "error"``.
    """

    def _get(obj: Any, key: str) -> Any:
        """Read ``key`` from a dict or an object, whichever ``obj`` is."""
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    status = _get(result, "status")
    parts: List[str] = []
    for block in _get(result, "content") or []:
        text = _get(block, "text")
        if isinstance(text, str):
            parts.append(text)
    joined = "".join(parts)
    if status == "error":
        raise RuntimeError(f"Neptune gateway tool error: {joined[:500]}")
    stripped = joined.strip()
    if stripped.startswith("{") and '"statusCode"' in stripped and '"body"' in stripped:
        try:
            body = json.loads(stripped).get("body")
            if isinstance(body, str):
                return body
        except (json.JSONDecodeError, TypeError):
            # Not an envelope after all — return the raw text below.
            pass
    if not joined:
        structured = _get(result, "structuredContent")
        if structured is not None:
            return json.dumps(structured)
    return joined


def build_gateway_client(*, gateway_url: str, region: str) -> Any:
    """Build a Strands MCP client bound to the Neptune Gateway.

    Imports ``strands`` / ``mcp_proxy_for_aws`` lazily so this module stays
    importable under the unit-test stubs with no AWS present.

    Parameters:
        gateway_url: The AgentCore gateway MCP endpoint (``NEPTUNE_GATEWAY_URL``).
        region: AWS region used to SigV4-sign the gateway calls.
    Returns:
        Any: An unopened Strands ``MCPClient``; the caller opens its session.
    """
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client  # noqa: PLC0415
    from strands.tools.mcp import MCPClient  # noqa: PLC0415

    return MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=gateway_url,
            aws_region=region,
            aws_service="bedrock-agentcore",
        )
    )


class NeptuneGatewayClient:
    """Calls the Neptune Gateway's tools: SPARQL, ontology fetch, Ontop translate.

    Named for the gateway rather than for CONSTRUCT because it now covers three
    of its tools. Phase 5 uses ``construct``; the governed-rule tier uses
    ``select_json`` (to read the rules graph), ``fetch_ontology`` and
    ``translate_sql`` — gateway capabilities and not
    VKG-agent ones — Ontop reformulates against whatever graph carries the
    ``vkg:mapsToTable`` / ``vkg:mapsToColumn`` predicates, and a hybrid layer's
    thin VKG carries exactly those.
    """

    def __init__(self, *, mcp_client: Any) -> None:
        """Construct the client around an MCP client with an ACTIVE session.

        Parameters:
            mcp_client: A Strands ``MCPClient`` for the Neptune Gateway whose
                session has already been opened (``with mcp_client:``).
        Returns:
            None.
        """
        self._mcp = mcp_client
        self._tool_names: Optional[Dict[str, str]] = None

    def _resolve(self, suffix: str) -> str:
        """Return the namespaced gateway tool name whose bare name is ``suffix``.

        Parameters:
            suffix: The bare tool name (e.g. ``"execute_sparql_query"``).
        Returns:
            str: The namespaced ``<target>___<tool>`` name.
        Raises:
            RuntimeError: The gateway exposes no such tool.
        """
        if self._tool_names is None:
            self._tool_names = {}
            for spec in self._mcp.list_tools_sync():
                name = getattr(spec, "tool_name", None) or getattr(spec, "name", None)
                if not name:
                    continue
                bare = name.split("___", 1)[1] if "___" in name else name
                self._tool_names[bare] = name
            logger.info("Neptune gateway tools: %s", sorted(self._tool_names.keys()))
        if suffix not in self._tool_names:
            raise RuntimeError(
                f"Neptune gateway does not expose tool {suffix!r}; available: "
                f"{sorted(self._tool_names.keys())}."
            )
        return self._tool_names[suffix]

    def construct(self, *, sparql: str) -> str:
        """Run a CONSTRUCT and return the resulting Turtle text.

        Parameters:
            sparql: The CONSTRUCT query text.
        Returns:
            str: The Turtle serialization of the constructed graph (may be empty
                when the slice matched nothing — Phase 5 treats an empty slice as
                ungrounded, which is the correct conservative outcome).
        Raises:
            RuntimeError: The tool errored or reported an ``error`` field.
        """
        name = self._resolve(SPARQL_TOOL)
        result = self._mcp.call_tool_sync(
            tool_use_id=f"vkg-construct-{uuid.uuid4().hex[:8]}",
            name=name,
            arguments={"sparql_query": sparql, "query_type": "CONSTRUCT"},
        )
        raw = _result_text(result)
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            # Some transports hand back raw Turtle rather than the JSON wrapper.
            return raw or ""
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"thin-VKG CONSTRUCT failed: {payload['error']}")
        if not isinstance(payload, dict):
            return raw or ""
        return payload.get("turtle", "")

    def select_json(self, *, sparql: str) -> Dict[str, Any]:
        """Run a SPARQL SELECT and return the RAW SPARQL-results JSON.

        Unflattened on purpose: ``rule_graph.read_rules`` rebuilds an RDF graph
        from the bindings, so it needs each term's ``type``/``datatype``. Flatten
        the bindings to their ``value`` strings and it can no longer tell an IRI
        from a literal, and every rule field comes back as a string.

        Parameters:
            sparql: The SELECT query text.
        Returns:
            Dict[str, Any]: ``{head: {vars}, results: {bindings}}``.
        Raises:
            RuntimeError: The tool is absent, the gateway returned a non-JSON
                body, or the payload carries an ``error``. Raising (not
                returning) is deliberate: an unreadable rules graph must not
                read as "this layer has no rules".
        """
        name = self._resolve(SPARQL_TOOL)
        result = self._mcp.call_tool_sync(
            tool_use_id=f"vkg-select-{uuid.uuid4().hex[:8]}",
            name=name,
            arguments={"sparql_query": sparql, "query_type": "SELECT"},
        )
        raw = _result_text(result)
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"SELECT returned non-JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"SELECT returned {type(payload).__name__}, expected an object."
            )
        if payload.get("error"):
            raise RuntimeError(f"SELECT failed: {payload['error']}")
        return payload

    def fetch_ontology(self, *, ontology_id: str) -> Dict[str, Any]:
        """Return the parsed ontology JSON for ``ontology_id``.

        The payload's ``mappings`` section is the part the rule tier needs: it is
        the OBDA table/column binding Ontop reformulates through. A hybrid layer's
        thin VKG populates it from the same ``vkg:mapsToTable`` /
        ``vkg:mapsToColumn`` triples a full VKG writes.

        Parameters:
            ontology_id: The semantic-layer id whose graph to read.
        Returns:
            Dict[str, Any]: ``{classes, properties, mappings, databases}``, or a
                payload carrying ``error`` when the graph is empty or missing.
                Returned rather than raised — an empty graph is a fact about the
                layer, and the caller decides whether it is fatal.
        Raises:
            RuntimeError: The tool is absent, or returned a non-JSON body.
        """
        name = self._resolve(ONTOLOGY_TOOL)
        result = self._mcp.call_tool_sync(
            tool_use_id=f"vkg-ontology-{uuid.uuid4().hex[:8]}",
            name=name,
            arguments={"ontology_id": ontology_id},
        )
        raw = _result_text(result)
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"{ONTOLOGY_TOOL} returned non-JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"{ONTOLOGY_TOOL} returned {type(payload).__name__}, expected an object."
            )
        logger.info(
            "fetch_ontology(%s): classes=%d properties=%d mappings=%d error=%s",
            ontology_id,
            len(payload.get("classes", {})),
            len(payload.get("properties", {})),
            len(payload.get("mappings", {})),
            payload.get("error"),
        )
        return payload

    def translate_sql(
        self,
        *,
        sparql: str,
        ontology_json: Dict[str, Any],
        ontology_id: str = "",
    ) -> Dict[str, Any]:
        """Translate SPARQL to Athena SQL via the gateway's Ontop tool.

        The graph is schema-only — no instance data — so a compiled rule's SPARQL
        is reformulated into SQL against the mapped relational tables, and that
        SQL is what runs on Athena. Note this is the ONE place the hybrid agent
        uses Ontop: its own Tier 2 generates SQL directly.

        Parameters:
            sparql: The compiled SPARQL SELECT to translate.
            ontology_json: The ``fetch_ontology`` payload, which carries the OBDA
                mappings Ontop needs.
            ontology_id: The layer id. Sent as ``ontologyId`` when non-empty so
                the Java handler keys its warm reformulator cache on a stable id
                instead of a content hash; omitted when empty.
        Returns:
            Dict[str, Any]: ``{sql, database, catalog}`` on success, or a payload
                carrying ``error`` when translation failed. The caller inspects
                ``error``/``sql``; an untranslatable rule is a legitimate outcome
                (design §2.1), not an exception.
        Raises:
            RuntimeError: The tool is absent, or returned a non-JSON body.
        """
        arguments: Dict[str, Any] = {"sparql": sparql, "ontologyJson": ontology_json}
        if ontology_id:
            arguments["ontologyId"] = ontology_id
        name = self._resolve(TRANSLATE_TOOL)
        result = self._mcp.call_tool_sync(
            tool_use_id=f"vkg-translate-{uuid.uuid4().hex[:8]}",
            name=name,
            arguments=arguments,
        )
        raw = _result_text(result)
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"{TRANSLATE_TOOL} returned non-JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"{TRANSLATE_TOOL} returned {type(payload).__name__}, expected an object."
            )
        logger.info(
            "translate_sql: sql=%s database=%s catalog=%s error=%s",
            bool(payload.get("sql")),
            payload.get("database"),
            payload.get("catalog"),
            payload.get("error"),
        )
        return payload
