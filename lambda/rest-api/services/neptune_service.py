"""
Neptune Service

This service handles Neptune graph operations using AgentCore Gateway
"""

import os
import logging
import json
import uuid
from typing import Callable, Dict, Any, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — imports only for the type checker
    from agents.shared.rule_models import Rule
    from strands.tools.mcp import MCPClient

logger = logging.getLogger(__name__)

# The gateway namespaces every tool as `<targetName>___<toolName>`. The SPARQL
# passthrough target is declared in
# cdk/lib/stacks/backend/agentcore/neptune-gateway-construct.ts as
# `execute-sparql-query` holding the tool `execute_sparql_query`.
_SPARQL_TOOL = 'execute-sparql-query___execute_sparql_query'


class NeptuneService:
    """Service for Neptune graph queries via AgentCore Gateway"""

    # Declared on the class, not only in __init__, so the `gateway_client`
    # property still works on instances built with `NeptuneService.__new__` —
    # tests do that to inject a stub without touching the environment.
    _gateway_client: Optional['MCPClient'] = None

    def __init__(self):
        """Initialize Neptune service with Gateway configuration"""
        self.neptune_gateway_url = os.environ.get('NEPTUNE_GATEWAY_URL')
        self.region = os.environ.get('AWS_REGION', 'us-east-1')
        # Built on first `gateway_client` access rather than here. `neptune_api`
        # constructs this service at module scope, so anything done in __init__ is
        # paid during Lambda's 10s init phase — and building the client means
        # importing `strands.tools.mcp` + `mcp_proxy_for_aws`, 37% of the REST
        # API's whole cold start, for a code path most requests never reach.
        self._gateway_client: Optional['MCPClient'] = None

        if not self.neptune_gateway_url:
            logger.warning("NEPTUNE_GATEWAY_URL environment variable not set")
        else:
            logger.info(f"NeptuneService configured with Gateway: {self.neptune_gateway_url}")

    @property
    def gateway_client(self) -> Optional['MCPClient']:
        """The MCP gateway client, built on first access and cached thereafter.

        Returns:
            An ``MCPClient`` authenticated with IAM against the Neptune AgentCore
            Gateway, or ``None`` when ``NEPTUNE_GATEWAY_URL`` is unset — callers
            already treat a falsy client as "gateway not configured" and raise.

        Caching matters beyond speed: callers do ``with self.gateway_client:`` and
        then call methods on ``self.gateway_client`` inside that block, so a fresh
        client per access would use a session that was never entered.
        """
        # An explicitly-assigned client wins over the environment, so injecting a
        # stub works even when NEPTUNE_GATEWAY_URL is unset.
        if self._gateway_client is not None:
            return self._gateway_client

        if not self.neptune_gateway_url:
            return None

        # Imported here, not at module scope — see the note in __init__.
        from strands.tools.mcp import MCPClient
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

        self._gateway_client = MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=self.neptune_gateway_url,
                aws_region=self.region,
                aws_service="bedrock-agentcore"
            )
        )
        logger.info(f"Neptune Gateway MCP client built: {self.neptune_gateway_url}")

        return self._gateway_client

    @gateway_client.setter
    def gateway_client(self, client: Optional['MCPClient']) -> None:
        """Assign the gateway client, overriding the lazily-built one.

        Args:
            client: A client to use, or None to reset so the next read rebuilds.

        Returns:
            None.

        Present so ``gateway_client`` stays an ordinary read/write attribute from
        every caller's point of view — making it a bare read-only property would
        break existing callers that inject a stub.
        """
        self._gateway_client = client

    def list_available_tools(self):
        """
        List all available tools from the Neptune Gateway

        Returns:
            List of tool names and their metadata
        """
        if not self.gateway_client:
            raise ValueError("Neptune Gateway not configured")

        try:
            with self.gateway_client:
                # Get all tools with pagination
                tools = []
                pagination_token = None
                more_tools = True

                while more_tools:
                    tmp_tools = self.gateway_client.list_tools_sync(pagination_token=pagination_token)
                    tools.extend(tmp_tools)

                    if hasattr(tmp_tools, 'pagination_token') and tmp_tools.pagination_token:
                        pagination_token = tmp_tools.pagination_token
                    else:
                        more_tools = False

                logger.info(f"Found {len(tools)} tools in Neptune Gateway")
                return [{'name': tool.tool_name, 'description': getattr(tool, 'description', '')} for tool in tools]

        except Exception as e:
            logger.warning(f"Failed to list tools: {str(e)}", exc_info=True)
            raise

    def _call_gateway_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call a Gateway tool via MCP client

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool response as dictionary
        """
        if not self.gateway_client:
            raise ValueError("Neptune Gateway not configured")

        try:
            # Generate unique tool use ID
            tool_use_id = f"{tool_name}-{uuid.uuid4()}"

            # Call tool via MCP client using context manager
            with self.gateway_client:
                response = self.gateway_client.call_tool_sync(
                    tool_use_id=tool_use_id,
                    name=tool_name,
                    arguments=arguments
                )

                # Extract output content from response.
                # call_tool_sync may return a plain dict OR an object with attributes.
                if isinstance(response, dict):
                    if 'content' in response:
                        result = response['content']
                    elif 'results' in response:
                        result = response['results']
                    elif 'output' in response:
                        result = response['output']
                    else:
                        result = response
                elif hasattr(response, "results") and response.results:
                    result = response.results
                elif hasattr(response, "output") and response.output:
                    result = response.output
                elif hasattr(response, "content"):
                    result = response.content
                else:
                    result = response

                # Parse content if it's a list with text
                if isinstance(result, list) and len(result) > 0:
                    if hasattr(result[0], 'text'):
                        parsed = json.loads(result[0].text)
                    elif isinstance(result[0], dict) and 'text' in result[0]:
                        parsed = json.loads(result[0]['text'])
                    else:
                        return result
                    # Handle double-nested response: {"statusCode": 200, "body": "...json string..."}
                    if isinstance(parsed, dict) and 'statusCode' in parsed and 'body' in parsed:
                        body = parsed['body']
                        if isinstance(body, str):
                            return json.loads(body)
                        return body
                    return parsed

                # Parse JSON response if string
                if isinstance(result, str):
                    return json.loads(result)

                return result

        except Exception as e:
            logger.warning(f"Gateway tool call failed for {tool_name}: {str(e)}", exc_info=True)
            raise

    def get_graph_summary(self, id: str) -> Dict[str, Any]:
        """
        Get summary for an ontology graph, including entities and properties lists.

        Aggregates data from get_graph_summary, get_graph_classes, and
        get_graph_properties tools so the frontend receives a single normalised
        response with:
          entities    – list of OWL classes {name, type, count, description}
          relationships – list of object properties {name, from, to, count}
          properties  – list of datatype properties {name, entity, dataType, description}

        Args:
            id: Ontology identifier

        Returns:
            Dictionary with summary statistics and entity/property arrays
        """
        try:
            summary = self._call_gateway_tool('get-graph-summary___get_graph_summary', {
                'ontology_id': id
            })
            classes_result = self._call_gateway_tool('get-graph-classes___get_graph_classes', {
                'ontology_id': id
            })
            props_result = self._call_gateway_tool('get-graph-properties___get_graph_properties', {
                'ontology_id': id
            })

            entities = [
                {
                    'name': c.get('label') or c.get('uri', '').split('/')[-1],
                    'type': 'Class',
                    'count': 0,
                    'description': c.get('comment', '-'),
                }
                for c in classes_result.get('classes', [])
            ]

            # Deduplicate by URI (Neptune can return duplicate class entries)
            seen_uris = set()
            unique_entities = []
            for c_raw, entity in zip(classes_result.get('classes', []), entities):
                uri = c_raw.get('uri', entity['name'])
                if uri not in seen_uris:
                    seen_uris.add(uri)
                    unique_entities.append(entity)

            properties = [
                {
                    'name': p.get('label') or p.get('uri', '').split('/')[-1],
                    'entity': p.get('domain', '').split('/')[-1] or '-',
                    'dataType': (p.get('range') or 'string').split('#')[-1],
                    'description': p.get('comment', '-'),
                    'mapsToColumn': p.get('mapsToColumn', ''),
                }
                for p in props_result.get('properties', [])
            ]

            # Object properties come back from the same gateway call
            relationships = [
                {
                    'name': r.get('name') or r.get('uri', '').split('/')[-1],
                    'from': r.get('from', '-'),
                    'to': r.get('to', '-'),
                    'count': 0,
                }
                for r in props_result.get('relationships', [])
            ]

            return {
                'id': id,
                'classCount': summary.get('classCount', 0),
                'propertyCount': summary.get('propertyCount', 0),
                'tripleCount': summary.get('tripleCount', 0),
                'entities': unique_entities,
                'relationships': relationships,
                'properties': properties,
            }

        except Exception as e:
            logger.error(f"Error getting graph summary: {e}")
            return {
                'id': id,
                'classCount': 0,
                'propertyCount': 0,
                'tripleCount': 0,
                'entities': [],
                'relationships': [],
                'properties': [],
                'error': str(e)
            }

    def get_graph_stats(self, id: str) -> Dict[str, Any]:
        """
        Get statistics for an ontology graph, normalised to the field names
        expected by the frontend:
          totalVertices  – OWL class count (conceptual graph nodes)
          totalEdges     – RDF triple count (statements / edges)
          totalClasses   – OWL class count
          totalProperties – datatype / object property count

        Args:
            id: Ontology identifier

        Returns:
            Dictionary with statistics
        """
        try:
            summary = self._call_gateway_tool('get-graph-summary___get_graph_summary', {
                'ontology_id': id
            })
            stats = self._call_gateway_tool('get-graph-stats___get_graph_stats', {
                'ontology_id': id
            })

            class_count = summary.get('classCount', 0)
            property_count = summary.get('propertyCount', 0)
            triple_count = summary.get('tripleCount', 0)

            return {
                'id': id,
                'totalVertices': class_count,
                'totalEdges': triple_count,
                'totalClasses': class_count,
                'totalProperties': property_count,
                'classDistribution': stats.get('classDistribution', []),
            }

        except Exception as e:
            logger.error(f"Error getting graph stats: {e}")
            return {
                'id': id,
                'totalVertices': 0,
                'totalEdges': 0,
                'totalClasses': 0,
                'totalProperties': 0,
                'classDistribution': [],
                'error': str(e)
            }

    def get_graph_classes(self, id: str) -> Dict[str, Any]:
        """
        Get list of all classes in the ontology

        Args:
            id: Ontology identifier

        Returns:
            List of classes with labels and comments
        """
        try:
            result = self._call_gateway_tool('get-graph-classes___get_graph_classes', {
                'ontology_id': id
            })
            return result

        except Exception as e:
            logger.error(f"Error getting graph classes: {e}")
            return {
                'id': id,
                'classes': [],
                'error': str(e)
            }

    def delete_graph(self, id: str) -> Dict[str, Any]:
        """
        Drop all triples in the named graph for a given id.

        Args:
            id: Ontology identifier (UUID)

        Returns:
            Dictionary with success status and triples_deleted count
        """
        try:
            result = self._call_gateway_tool('delete-graph___delete_graph', {
                'ontology_id': id
            })
            return result

        except Exception as e:
            logger.error(f"Error deleting Neptune graph for ontology {id}: {e}")
            return {
                'success': False,
                'id': id,
                'error': str(e)
            }

    def get_graph_properties(self, id: str) -> Dict[str, Any]:
        """
        Get list of all properties in the ontology

        Args:
            id: Ontology identifier

        Returns:
            List of properties with labels and comments
        """
        try:
            result = self._call_gateway_tool('get-graph-properties___get_graph_properties', {
                'ontology_id': id
            })
            return result

        except Exception as e:
            logger.error(f"Error getting graph properties: {e}")
            return {
                'id': id,
                'properties': [],
                'error': str(e)
            }

    # ================================================================
    # Governed rules (enableGovernedRules) — the <ontologyId>/rules graph
    # ================================================================
    # Every SPARQL string below is built by agents/shared/rule_graph.py, so the
    # REST Lambda and the agent runtime read and write rules through ONE
    # implementation of the vocabulary. These methods are transport only.
    #
    # Transport is the existing execute_sparql_query gateway tool — no new
    # gateway target and no new IAM. Rules live in a SIBLING named graph
    # (<ontology-graph>/rules), which _resolve_graph_uri in
    # lambda/neptune-tools/index.py explicitly excludes so rule triples can
    # never leak into an ontology slice.

    def write_rule(self, *, rule: "Rule") -> None:
        """Upsert one rule into its namespace's rules graph.

        Emits DELETE-then-INSERT in a single update so an edited rule replaces
        its previous triples. Without the DELETE the blank-node condition and
        threshold children would accumulate, and the rule would read back with
        its conditions duplicated.

        Args:
            rule: The rule to persist.

        Returns:
            None.
        """
        from agents.shared.rule_graph import rule_to_upsert_update

        self._call_gateway_tool(_SPARQL_TOOL, {
            'sparql_query': rule_to_upsert_update(rule=rule),
            'query_type': 'UPDATE',
        })

    def read_rules(self, *, namespace: str) -> List["Rule"]:
        """Read every rule in a namespace's rules graph.

        Args:
            namespace: Ontology id owning the rules graph.

        Returns:
            List[Rule]: The parsed rules; empty when the graph does not exist.
        """
        from agents.shared.rule_graph import read_rules as _read_rules

        return _read_rules(
            namespace=namespace,
            execute_sparql=self._rule_sparql_executor(),
        )

    def read_rules_partitioned(
        self, *, namespace: str,
    ) -> Tuple[List["Rule"], List[Dict[str, str]]]:
        """Read a namespace's rules, isolating any that cannot be deserialized.

        Same query as read_rules; only the reassembly differs. Used by the list
        endpoint so one corrupt rule cannot 500 the whole namespace.

        Args:
            namespace: Ontology id owning the rules graph.

        Returns:
            Tuple[List[Rule], List[Dict[str, str]]]: The readable rules, and one
                {"rule_id", "error"} map per unreadable rule.
        """
        from agents.shared.rule_graph import read_rules_partitioned

        return read_rules_partitioned(
            namespace=namespace,
            execute_sparql=self._rule_sparql_executor(),
        )

    def _rule_sparql_executor(self) -> Callable[..., Dict[str, Any]]:
        """Build the injected SPARQL executor the rule readers expect.

        Returns:
            Callable: Takes `query` and `query_type` keywords and returns parsed
                SPARQL-results JSON from the gateway tool.
        """
        return lambda *, query, query_type: self._call_gateway_tool(
            _SPARQL_TOOL,
            {'sparql_query': query, 'query_type': query_type},
        )

    def delete_rule(self, *, namespace: str, rule_id: str) -> None:
        """Delete one rule and its child nodes from the rules graph.

        Args:
            namespace: Ontology id owning the rules graph.
            rule_id: Identifier of the rule to remove.

        Returns:
            None.
        """
        from agents.shared.rule_graph import delete_rule_update

        self._call_gateway_tool(_SPARQL_TOOL, {
            'sparql_query': delete_rule_update(
                namespace=namespace, rule_id=rule_id,
            ),
            'query_type': 'UPDATE',
        })

    def ask(self, *, query: str, graph_uri: str) -> bool:
        """Run a SPARQL ASK scoped to one named graph.

        Used by rule validation level 2 (does the rule's subject class actually
        declare this property as its ``rdfs:domain``?). The caller writes a
        graph-agnostic pattern and this method wraps it, so no validator has to
        know how graphs are named.

        FAIL-CLOSED: a response without a ``boolean`` key reads as False. A
        malformed answer must never let an unchecked rule through the gate.

        Args:
            query: An ASK query; its pattern is re-wrapped in a GRAPH clause.
            graph_uri: The named graph to evaluate against.

        Returns:
            bool: The ASK result, or False if it cannot be determined.
        """
        pattern = query.strip()
        # Rewrite `ASK { <pattern> }` as `ASK { GRAPH <g> { <pattern> } }`.
        # Slicing on the first `{` and last `}` keeps nested braces (OPTIONAL,
        # FILTER blocks) intact.
        inner = pattern[pattern.index('{') + 1:pattern.rindex('}')]
        scoped = f'ASK {{ GRAPH <{graph_uri}> {{{inner}}} }}'
        result = self._call_gateway_tool(_SPARQL_TOOL, {
            'sparql_query': scoped,
            'query_type': 'SELECT',
        })
        if isinstance(result, dict):
            return bool(result.get('boolean', False))
        return False

    def select_in_graph(self, *, query: str, graph_uri: str) -> List[Dict[str, str]]:
        """Run a SPARQL SELECT scoped to one named graph, flattened to dicts.

        Used by rule validation level 3, which walks ``vkg:mapsToColumn`` to
        decide whether every binding a rule needs is actually mapped to a
        column.

        Args:
            query: A SELECT query; its WHERE block is re-wrapped in a GRAPH
                clause.
            graph_uri: The named graph to query.

        Returns:
            List[Dict[str, str]]: One dict per row, ``{varName: value}``. Empty
                on an unparseable response — level 3 reads "no rows" as
                "unmapped", which is the fail-closed direction.
        """
        where_start = query.index('{')
        head = query[:where_start]
        inner = query[where_start + 1:query.rindex('}')]
        scoped = f'{head}{{ GRAPH <{graph_uri}> {{{inner}}} }}'
        result = self._call_gateway_tool(_SPARQL_TOOL, {
            'sparql_query': scoped,
            'query_type': 'SELECT',
        })
        bindings = (result or {}).get('results', {}).get('bindings', [])
        return [
            {name: cell.get('value', '') for name, cell in row.items()}
            for row in bindings
        ]

    def resolve_ontology_graph_uri(self, *, id: str) -> str:
        """Resolve the NEWEST ontology named-graph URI for a layer id.

        The rule validators must query the graph the ontology actually lives in,
        and that URI cannot be constructed from a prefix: the creation agent
        writes triples under ``http://<slug-of-layer-name>/ontology/<id>``, where
        the slug carries a build timestamp. So it is discovered, exactly as
        lambda/neptune-tools/index.py `_resolve_graph_uris` does:

        - Filter by the id, because the slug is not knowable here.
        - Exclude ``/rules``, or ontology validation would read rule triples.
        - Sort DESCENDING and take the first. A rebuild MINTS A NEW GRAPH and
          the superseded one survives, so without an explicit order an arbitrary
          stale build answers — that is what made a rebuilt layer look unchanged.

        Args:
            id: Ontology/layer identifier (UUID).

        Returns:
            str: The newest matching ontology graph URI.

        Raises:
            ValueError: If no ontology graph exists for this id. Validating
                against a nonexistent graph would fail every level for the wrong
                reason ("property not declared") instead of naming the real
                cause ("the layer was never built").
        """
        result = self._call_gateway_tool(
            'discover-named-graphs___discover_named_graphs', {},
        )
        graphs = (result or {}).get('named_graphs', [])
        matching = sorted(
            (g for g in graphs if id in g and not g.endswith('/rules')),
            reverse=True,
        )
        if not matching:
            raise ValueError(
                f"no ontology named graph exists in Neptune for id '{id}' — "
                f"build the semantic layer before authoring rules against it"
            )
        if len(matching) > 1:
            logger.warning(
                "%d ontology graphs exist for '%s' (superseded rebuilds were "
                "never dropped) — using the newest, %s",
                len(matching), id, matching[0],
            )
        return matching[0]

    def get_ontology(self, *, id: str) -> Dict[str, Any]:
        """Fetch the ontology payload Ontop needs for reformulation.

        Args:
            id: Ontology identifier.

        Returns:
            Dict[str, Any]: ``{classes, properties, mappings, databases}``.
        """
        return self._call_gateway_tool(
            'get-ontology-from-neptune___get_ontology_from_neptune',
            {'ontology_id': id},
        )

    def translate_sparql_to_sql(
        self, *, sparql: str, ontology_id: str, ontology_json: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Translate SPARQL to Athena SQL via the Ontop translate Lambda.

        Used by the publish gate's smoke-execution step: a rule Ontop cannot
        translate must never reach PUBLISHED.

        Args:
            sparql: The grounded SPARQL SELECT.
            ontology_id: Reformulator cache key.
            ontology_json: The ``get_ontology`` payload — the translate Lambda
                has no Neptune access of its own, so the caller supplies it.

        Returns:
            Dict[str, Any]: ``{sql, database, catalog}``.
        """
        return self._call_gateway_tool(
            'translate-sparql-to-sql___translate_sparql_to_sql',
            {
                'sparql': sparql,
                'ontologyId': ontology_id,
                'ontologyJson': ontology_json,
            },
        )
