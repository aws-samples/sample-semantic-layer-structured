"""Neptune thin-VKG loader for the Hybrid creation agent (gateway path).

The Hybrid creation agent reaches Neptune **only** through the AgentCore Neptune
Gateway — it has no direct SigV4 access and is not attached to Neptune's VPC. The
gateway's ``persist_to_neptune`` tool parses N-Quads, groups them by named graph,
and issues ``INSERT DATA`` per graph.

Copy-and-own: the MCP result-unwrapping and tool-name-resolution logic is copied
in shape from ``ontology_query_agent/tier2/gateway_client.py``. It is deliberately
NOT imported — ``tests/unit/test_hybrid_isolation.py`` forbids this package from
importing ``agents.shared`` or any sibling agent package, so the hybrid stack can
be extracted or redeployed independently. Only the standard library is imported at
module scope; ``strands`` / ``mcp_proxy_for_aws`` are imported lazily inside
:func:`build_gateway_client` so the module imports fine in unit tests with no AWS
present.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The gateway tool that writes N-Quads into Neptune (bare, un-namespaced name).
PERSIST_TOOL = "persist_to_neptune"

# The gateway tool that drops EVERY named graph belonging to a layer. Required
# before a re-load because ``persist_to_neptune`` issues ``INSERT DATA``, which
# ADDS to whatever is already in the graph — see :meth:`NeptuneLoadClient.clear`.
DELETE_TOOL = "delete_graph"

# MUST stay identical to NQUAD_PATTERN in lambda/neptune-tools/index.py, the
# grammar its tool_persist_to_neptune accepts. That parser rejects the WHOLE
# payload if any line fails to match, so a subtly-malformed quad fails the load
# rather than half-writing it. We validate against the same grammar BEFORE
# sending so the build reports which line is at fault instead of a bare
# gateway error.
#
# The literal alternative allows backslash escapes: descriptions harvested from
# the KB documents routinely contain double quotes, which vkg_builder escapes as
# \" — legal N-Quads that a `"[^"]*"` pattern would reject.
#
# The IRI class is `[^\x00-\x20<>"{}|^`\\]` — SPARQL's IRIREF production — and
# NOT the laxer `[^>]+` that N-Quads alone permits. That difference is the whole
# point: a column named "academic year" yields <.../frpm/academic year>, which
# `[^>]+` accepts and Neptune's INSERT DATA then rejects with a
# MalformedQueryException pointing at the vocabulary host, ~15 minutes of build
# work after the mistake was made. Matching SPARQL's grammar here means the
# offending line is named before anything is sent.
_IRIREF = r'<[^\x00-\x20<>"{}|^`\\]+>'
_NQUAD_PATTERN = re.compile(
    rf'^({_IRIREF})\s+({_IRIREF})\s+'
    rf'({_IRIREF}|"(?:[^"\\]|\\.)*"(?:\^\^{_IRIREF}|@[A-Za-z][A-Za-z0-9-]*)?)'
    rf'\s+({_IRIREF})\s*\.\s*$'
)


def validate_nquads(nquads: str) -> int:
    """Check every N-Quads line is loadable by the gateway; return the line count.

    Parameters:
        nquads: The N-Quads text about to be sent to ``persist_to_neptune``.
    Returns:
        int: The number of quads that will be loaded.
    Raises:
        ValueError: At least one non-blank line does not match the gateway's
            accepted grammar. The gateway refuses the whole payload in that case,
            so checking here turns an opaque remote rejection into an error that
            names the offending line.
    """
    lines = [line.strip() for line in nquads.strip().split("\n")]
    non_blank = [line for line in lines if line]
    bad = [line for line in non_blank if not _NQUAD_PATTERN.match(line)]
    if bad:
        raise ValueError(
            f"{len(bad)} of {len(non_blank)} thin-VKG N-Quads lines would be "
            f"REJECTED by the gateway's parser, which refuses the whole payload "
            f"rather than loading a partial graph. First offender: "
            f"{bad[0][:200]!r}"
        )
    return len(non_blank)


def _result_text(result: Any) -> str:
    """Concatenate an MCP tool result's text blocks into one string.

    Handles the two shapes the gateway transport can produce: plain text content
    blocks, and the Lambda ``{statusCode, body}`` envelope that some transports
    surface without unwrapping.

    Parameters:
        result: The ``MCPToolResult`` returned by ``call_tool_sync`` (dict-like or
            attribute-like — both are supported).
    Returns:
        str: The tool's payload text, with the Lambda envelope peeled if present.
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
    # Peel the Lambda proxy envelope so callers always see the inner payload.
    stripped = joined.strip()
    if stripped.startswith("{") and '"statusCode"' in stripped and '"body"' in stripped:
        try:
            body = json.loads(stripped).get("body")
            if isinstance(body, str):
                return body
        except (json.JSONDecodeError, TypeError):
            # Not actually an envelope — fall through and return the raw text.
            pass
    if not joined:
        # Some transports return only structuredContent for JSON-shaped results.
        structured = _get(result, "structuredContent")
        if structured is not None:
            return json.dumps(structured)
    return joined


def build_gateway_client(*, gateway_url: str, region: str) -> Any:
    """Build a Strands MCP client bound to the Neptune Gateway.

    Imports ``strands`` / ``mcp_proxy_for_aws`` lazily so this module stays
    importable in unit tests (and in any environment without the agent runtime's
    dependencies installed).

    Parameters:
        gateway_url: The AgentCore gateway MCP endpoint (``NEPTUNE_GATEWAY_URL``).
        region: AWS region used to SigV4-sign the gateway calls.
    Returns:
        Any: An unopened Strands ``MCPClient``; the caller must open its session.
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


class NeptuneLoadClient:
    """Writes thin-VKG N-Quads into Neptune via the gateway's persist tool."""

    def __init__(self, *, mcp_client: Any) -> None:
        """Construct the loader around an MCP client with an ACTIVE session.

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

        Gateway targets expose tools as ``<target>___<tool>``, so the bare name is
        not directly callable. The tool list is fetched once and cached.

        Parameters:
            suffix: The bare tool name (e.g. ``"persist_to_neptune"``).
        Returns:
            str: The namespaced tool name to pass to ``call_tool_sync``.
        Raises:
            RuntimeError: The gateway does not expose a tool with that bare name.
                Failing here is much clearer than a downstream "unknown tool".
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
                f"{sorted(self._tool_names.keys())}. The thin-VKG load cannot proceed."
            )
        return self._tool_names[suffix]

    def clear(self, *, ontology_id: str) -> Dict[str, Any]:
        """Drop the layer's existing named graph(s) so a re-load REPLACES them.

        ``persist_to_neptune`` issues ``INSERT DATA``, which adds to whatever is
        already stored. RDF is a set, so re-inserting an unchanged graph is
        idempotent and this looked harmless — but anything the new build no longer
        emits SURVIVES. A rebuild that renames a relationship (an inferred edge
        going from ``hasForeignData`` to ``hasForeignDataViaUuid`` once a second
        edge to the same target appears) or drops one it no longer infers leaves
        the superseded property in the graph, and the viewer and the query side
        then both see edges the current build never produced.

        Parameters:
            ontology_id: The layer id embedded in the graph URIs to drop.
        Returns:
            dict: The parsed ``{"success": bool, "graphs_deleted": int, ...}``
                payload. ``graphs_deleted == 0`` is a normal first build.
        Raises:
            ValueError: ``ontology_id`` is blank.
            RuntimeError: The gateway lacks the tool, errored, returned non-JSON,
                or reported ``success`` falsy. Fail loud rather than load on top of
                a graph we could not clear — the result would silently mix two
                builds, which is worse than a failed build.
        """
        if not ontology_id.strip():
            raise ValueError("ontology_id is required to clear a layer's graph")
        name = self._resolve(DELETE_TOOL)
        result = self._mcp.call_tool_sync(
            tool_use_id=f"vkg-clear-{uuid.uuid4().hex[:8]}",
            name=name,
            arguments={"ontology_id": ontology_id},
        )
        raw = _result_text(result)
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"{DELETE_TOOL} returned non-JSON: {exc}; raw={raw[:300]!r}"
            ) from exc
        if not payload.get("success"):
            raise RuntimeError(
                f"Clearing the previous thin VKG failed: "
                f"{payload.get('message') or payload.get('error') or raw[:300]!r}"
            )
        logger.info(
            "cleared %s graph(s) / %s triple(s) for layer %s",
            payload.get("graphs_deleted"),
            payload.get("triples_deleted"),
            ontology_id,
        )
        return payload

    def persist(self, *, nquads: str) -> Dict[str, Any]:
        """Load N-Quads into Neptune and return the tool's parsed result.

        Parameters:
            nquads: The thin-VKG N-Quads text. Each line must carry an explicit
                named graph (4th position) — the gateway groups by that graph.
        Returns:
            dict: The parsed ``{"success": bool, "message": str}`` payload.
        Raises:
            ValueError: ``nquads`` is empty, or any line would be silently dropped
                by the gateway's parser (see :func:`validate_nquads`). Loading
                nothing — or only part — must never be mistaken for success.
            RuntimeError: The tool errored, returned non-JSON, or reported
                ``success`` falsy. A failed load must not leave the layer looking
                ready, so this propagates.
        """
        if not nquads.strip():
            raise ValueError(
                "Refusing to load an empty thin VKG into Neptune — an empty graph "
                "cannot ground any query, so this is a build failure, not a no-op."
            )
        expected = validate_nquads(nquads)
        name = self._resolve(PERSIST_TOOL)
        result = self._mcp.call_tool_sync(
            tool_use_id=f"vkg-load-{uuid.uuid4().hex[:8]}",
            name=name,
            arguments={"nquad_data": nquads},
        )
        raw = _result_text(result)
        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(
                f"{PERSIST_TOOL} returned non-JSON: {exc}; raw={raw[:300]!r}"
            ) from exc
        if not payload.get("success"):
            raise RuntimeError(
                f"Thin-VKG load into Neptune failed: "
                f"{payload.get('message') or payload.get('error') or raw[:300]!r}"
            )
        logger.info(
            "thin-VKG load ok (%d quads sent): %s", expected, payload.get("message")
        )
        return payload
