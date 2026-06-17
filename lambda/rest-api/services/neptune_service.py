"""
Neptune Service

This service handles Neptune graph operations using AgentCore Gateway
"""

import os
import logging
import json
import uuid
from typing import Dict, Any
from strands.tools.mcp import MCPClient
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

logger = logging.getLogger(__name__)


class NeptuneService:
    """Service for Neptune graph queries via AgentCore Gateway"""

    def __init__(self):
        """Initialize Neptune service with Gateway configuration"""
        self.neptune_gateway_url = os.environ.get('NEPTUNE_GATEWAY_URL')
        self.region = os.environ.get('AWS_REGION', 'us-east-1')

        if not self.neptune_gateway_url:
            logger.warning("NEPTUNE_GATEWAY_URL environment variable not set")
            self.gateway_client = None
        else:
            # Initialize MCP client with IAM authentication
            self.gateway_client = MCPClient(
                lambda: aws_iam_streamablehttp_client(
                    endpoint=self.neptune_gateway_url,
                    aws_region=self.region,
                    aws_service="bedrock-agentcore"
                )
            )
            logger.info(f"NeptuneService initialized with Gateway: {self.neptune_gateway_url}")

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
