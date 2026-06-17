"""
Neptune Tools Lambda Function for AgentCore Gateway
Provides MCP tool interface to Neptune SPARQL operations
"""

import json
import boto3
import os
import logging
import requests
import re
from collections import defaultdict
from typing import Dict, Any
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Reduce AWS SDK noise
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)


def _validate_ontology_id(ontology_id: str) -> str:
    """Validate ontology_id is safe for use in SPARQL query construction."""
    if not re.match(r'^[a-zA-Z0-9_\-]+$', str(ontology_id)):
        raise ValueError(f"Invalid ontology_id format: {ontology_id!r}")
    return ontology_id


def get_neptune_endpoint() -> str:
    """Get Neptune SPARQL endpoint (full URL with scheme) from Secrets Manager.

    The secret may contain either:
      - 'sparqlEndpoint': full URL already (preferred)
      - 'endpoint':       bare hostname — we prepend https://

    A bare hostname without a scheme causes botocore's URL parser to return
    None for the host component, which makes SigV4Auth crash with
    'NoneType' object has no attribute 'split'.
    """
    try:
        region = os.environ.get('AWS_REGION', 'us-east-1')
        secret_name = os.environ.get('NEPTUNE_SECRET_NAME')

        secrets_client = boto3.client('secretsmanager', region_name=region)
        response = secrets_client.get_secret_value(SecretId=secret_name)
        secret_data = json.loads(response['SecretString'])

        # Prefer the pre-built sparqlEndpoint (already has https:// and port).
        # Strip the /sparql path suffix so callers get the base URL and append
        # /sparql themselves (matching the existing execute_sparql_query logic).
        sparql_ep = secret_data.get('sparqlEndpoint', '')
        if sparql_ep:
            base = sparql_ep[:-len('/sparql')] if sparql_ep.endswith('/sparql') else sparql_ep
            return base

        # Fall back to bare hostname — ensure it has a scheme
        endpoint = secret_data.get('endpoint', '')
        if endpoint and not endpoint.startswith('http'):
            endpoint = f"https://{endpoint}"
        return endpoint

    except Exception as e:
        logger.error(f"Failed to retrieve Neptune endpoint: {str(e)}")  # nosemgrep: logging-error-without-handling — handled via env var fallback; raises only if fallback also unavailable
        # Fallback to environment variable
        endpoint = os.environ.get('NEPTUNE_ENDPOINT', '')
        if not endpoint:
            raise Exception("Neptune endpoint not available")
        if not endpoint.startswith('http'):
            endpoint = f"https://{endpoint}"
        return endpoint


def execute_sparql_query(query: str) -> Dict[str, Any]:
    """Execute SPARQL query against Neptune with SigV4 auth"""
    neptune_endpoint = get_neptune_endpoint()
    region = os.environ.get('AWS_REGION', 'us-east-1')

    # Build SPARQL endpoint URL
    if ':8182' not in neptune_endpoint:
        sparql_url = f"{neptune_endpoint}:8182/sparql"
    else:
        sparql_url = f"{neptune_endpoint}/sparql"

    headers = {
        'Content-Type': 'application/sparql-query',
        'Accept': 'application/json'
    }

    # Get AWS credentials and sign request
    session = boto3.Session()
    credentials = session.get_credentials()

    request = AWSRequest(
        method='POST',
        url=sparql_url,
        data=query,
        headers=headers
    )

    SigV4Auth(credentials, 'neptune-db', region).add_auth(request)

    # Execute signed request
    response = requests.post(
        sparql_url,
        headers=dict(request.headers),
        data=query,
        timeout=30
    )

    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"SPARQL query failed: {response.status_code} - {response.text}")


# Stable VKG vocabulary namespace — must match VIRTUAL_KG_VOCAB in prompt_builder.py
VIRTUAL_KG_VOCAB = "https://semantic-layer.aws/virtual-kg/"


def _resolve_graph_uri(ontology_id: str) -> str:
    """Discover the named-graph URI for a given ontology_id.

    The agent stores triples under a graph URI of the form:
        http://{ontology_name}/ontology/{ontology_id}
    where {ontology_name} is the user-supplied name (e.g. "demo").
    Rather than looking up the name from DynamoDB, we query Neptune for any
    named graph whose URI contains the UUID — this works for any name.
    """
    sparql_query = f"""
    SELECT DISTINCT ?g
    WHERE {{
        GRAPH ?g {{ ?s ?p ?o }}
        FILTER(CONTAINS(STR(?g), "{ontology_id}"))
    }}
    LIMIT 1
    """
    try:
        result = execute_sparql_query(sparql_query)
        bindings = result.get('results', {}).get('bindings', [])
        if bindings:
            return bindings[0]['g']['value']
    except Exception as e:
        logger.warning(f"Could not discover graph URI for {ontology_id}: {e}")
    # Unreachable in normal operation, but keeps every call-site safe
    raise ValueError(f"No named graph found in Neptune for ontology_id '{ontology_id}'")


def tool_discover_named_graphs() -> str:
    """Discover all named graphs in Neptune"""
    try:
        sparql_query = """
        SELECT DISTINCT ?graph
        WHERE {
          GRAPH ?graph {
            ?s ?p ?o
          }
        }
        ORDER BY ?graph
        """

        result = execute_sparql_query(sparql_query)

        graphs = []
        if 'results' in result and 'bindings' in result['results']:
            for binding in result['results']['bindings']:
                graphs.append(binding['graph']['value'])

        return json.dumps({
            "named_graphs": graphs,
            "count": len(graphs)
        }, indent=2)

    except Exception as e:
        logger.error(f"Error discovering named graphs: {str(e)}")
        return json.dumps({"error": str(e)})


def tool_get_ontology_from_neptune(ontology_id: str) -> str:
    """Read ontology from Neptune for a specific ontology ID"""
    try:
        graph_uri = _resolve_graph_uri(ontology_id)

        sparql_query = f"""
        SELECT DISTINCT ?subject ?predicate ?object
        WHERE {{
            GRAPH <{graph_uri}> {{
                ?subject ?predicate ?object .
            }}
        }}
        ORDER BY ?subject ?predicate
        """

        result = execute_sparql_query(sparql_query)

        ontology_info = {
            "ontology_id": ontology_id,
            "graph_uri": graph_uri,
            "databases": [],
            "classes": {},
            "properties": {},
            "mappings": {}
        }
        _catalog_map: Dict[str, str] = {}
        _data_source_map: Dict[str, str] = {}
        _db_names: list = []

        if 'results' in result and 'bindings' in result['results']:
            for binding in result['results']['bindings']:
                subject = binding['subject']['value']
                predicate = binding['predicate']['value']
                obj = binding['object']['value']

                # Parse OWL classes and properties
                if predicate == 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type':
                    if obj == 'http://www.w3.org/2002/07/owl#Class':
                        ontology_info['classes'].setdefault(subject, {})
                    elif obj in ['http://www.w3.org/2002/07/owl#DatatypeProperty',
                                 'http://www.w3.org/2002/07/owl#ObjectProperty']:
                        existing = ontology_info['properties'].setdefault(subject, {})
                        existing['type'] = obj

                # rdfs:label / rdfs:comment — without these the schema digest
                # consumed by the query agent's column picker is empty, which
                # collapses Pick-2 to surface-level table-name matching.
                elif predicate in (
                    'http://www.w3.org/2000/01/rdf-schema#label',
                    'http://www.w3.org/2000/01/rdf-schema#comment',
                ):
                    field = 'label' if predicate.endswith('#label') else 'comment'
                    if subject in ontology_info['classes']:
                        ontology_info['classes'][subject][field] = obj
                    elif subject in ontology_info['properties']:
                        ontology_info['properties'][subject][field] = obj
                    else:
                        # Type triple hasn't been seen yet (rare given ORDER BY,
                        # but defensive). Park on properties.
                        ontology_info['properties'].setdefault(subject, {})[field] = obj

                # Curated business-context annotations the ontology_agent emits
                # per owl:Class (businessPurpose / businessConcepts /
                # acordSourcePath / referenceTables / commonQueryPatterns /
                # sampleData / notes). They live under the VKG vocab namespace
                # alongside mapsToTable. Surface them on the class dict so the
                # query agent's advisory path can ground on real schema context
                # (without them advisory had no KB → "this layer is empty").
                elif predicate.startswith(VIRTUAL_KG_VOCAB) and predicate.rsplit('/', 1)[1] in (
                    'businessPurpose', 'businessConcepts', 'acordSourcePath',
                    'referenceTables', 'commonQueryPatterns', 'sampleData', 'notes',
                ):
                    annotation = predicate.rsplit('/', 1)[1]
                    # Park on the class dict; classes are typed first thanks to
                    # ORDER BY ?subject ?predicate (rdf:type sorts before the vkg:
                    # annotation predicates for the same subject). setdefault is
                    # defensive in case the type triple hasn't been seen yet.
                    ontology_info['classes'].setdefault(subject, {})[annotation] = obj

                # Parse database metadata triple written at ontology level
                elif predicate == f'{VIRTUAL_KG_VOCAB}hasDatabase':
                    if obj not in _db_names:
                        _db_names.append(obj)

                # Parse catalog triple: value is "database_name::catalog_id"
                elif predicate == f'{VIRTUAL_KG_VOCAB}hasCatalog':
                    if '::' in obj:
                        db_name, catalog_id = obj.split('::', 1)
                        _catalog_map[db_name] = catalog_id

                # Parse data source triple: value is "database_name::athena_data_source"
                elif predicate == f'{VIRTUAL_KG_VOCAB}hasDataSource':
                    if '::' in obj:
                        db_name, data_source = obj.split('::', 1)
                        _data_source_map[db_name] = data_source

                # Parse traceability mappings
                elif 'mapsToTable' in predicate:
                    ontology_info['mappings'][subject] = {'table': obj}
                elif 'mapsToColumn' in predicate:
                    if subject in ontology_info['mappings']:
                        ontology_info['mappings'][subject]['column'] = obj
                    else:
                        ontology_info['mappings'][subject] = {'column': obj}

        ontology_info['databases'] = [
            {
                "name": db,
                "catalog": _catalog_map.get(db),
                "dataSource": _data_source_map.get(db),
            }
            for db in _db_names
        ]

        if not ontology_info['classes'] and not ontology_info['properties']:
            return json.dumps({
                "error": f"No ontology data found for ontology_id '{ontology_id}'",
                "graph_uri": graph_uri
            })

        return json.dumps(ontology_info)

    except Exception as e:
        logger.error(f"Error retrieving ontology: {str(e)}")
        return json.dumps({"error": str(e)})


def tool_delete_graph(ontology_id: str) -> str:
    """Delete (drop) all triples in the named graph for a given ontology_id"""
    try:
        ontology_id = _validate_ontology_id(ontology_id)
        graph_uri = _resolve_graph_uri(ontology_id)

        # Count triples before deletion so we can report how many were removed
        count_query = (f"""
        SELECT (COUNT(*) AS ?tripleCount)
        FROM <{graph_uri}>
        WHERE {{ ?s ?p ?o . }}
        """)  # nosec B608 - ontology_id validated by _validate_ontology_id; JWT-authenticated endpoint
        count_result = execute_sparql_query(count_query)
        bindings = count_result.get('results', {}).get('bindings', [])
        triple_count = int(bindings[0].get('tripleCount', {}).get('value', 0)) if bindings else 0

        if triple_count == 0:
            return json.dumps({
                "success": True,
                "ontology_id": ontology_id,
                "graph_uri": graph_uri,
                "triples_deleted": 0,
                "message": f"Graph '{graph_uri}' was already empty or did not exist",
            })

        # Execute DROP GRAPH via SPARQL Update endpoint
        neptune_endpoint = get_neptune_endpoint()
        region = os.environ.get('AWS_REGION', 'us-east-1')

        if ':8182' not in neptune_endpoint:
            sparql_url = f"{neptune_endpoint}:8182/sparql"
        else:
            sparql_url = f"{neptune_endpoint}/sparql"

        drop_query = f"DROP SILENT GRAPH <{graph_uri}>"  # nosec B608 - ontology_id validated by _validate_ontology_id; JWT-authenticated endpoint

        headers = {
            'Content-Type': 'application/sparql-update',
            'Accept': 'application/json',
        }

        session = boto3.Session()
        credentials = session.get_credentials()

        request = AWSRequest(
            method='POST',
            url=sparql_url,
            data=drop_query,
            headers=headers,
        )
        SigV4Auth(credentials, 'neptune-db', region).add_auth(request)

        response = requests.post(
            sparql_url,
            headers=dict(request.headers),
            data=drop_query,
            timeout=30,
        )

        if response.status_code == 200:
            return json.dumps({
                "success": True,
                "ontology_id": ontology_id,
                "graph_uri": graph_uri,
                "triples_deleted": triple_count,
                "message": f"Deleted {triple_count} triples from graph '{graph_uri}'",
            })
        else:
            return json.dumps({
                "success": False,
                "ontology_id": ontology_id,
                "graph_uri": graph_uri,
                "message": f"HTTP {response.status_code}: {response.text}",
            })

    except Exception as e:
        logger.error(f"Error deleting graph: {str(e)}")
        return json.dumps({"success": False, "error": str(e)})


def tool_execute_sparql_query(sparql_query: str, query_type: str = "SELECT") -> str:
    """
    Execute a generic SPARQL query against Neptune

    Args:
        sparql_query: SPARQL query string to execute
        query_type: Type of query - "SELECT" or "UPDATE" (default: "SELECT")

    Returns:
        JSON string with query results or execution status
    """
    try:
        neptune_endpoint = get_neptune_endpoint()
        region = os.environ.get('AWS_REGION', 'us-east-1')

        # Build SPARQL endpoint URL
        if ':8182' not in neptune_endpoint:
            sparql_url = f"{neptune_endpoint}:8182/sparql"
        else:
            sparql_url = f"{neptune_endpoint}/sparql"

        # Set content type + Accept based on query type. CONSTRUCT/DESCRIBE
        # return an RDF graph (not a SPARQL-results table), so we ask Neptune
        # for Turtle and hand the raw body back to the caller — this is what the
        # VKG Tier 2 graph workflow's Phase 3 slice builder consumes.
        qt = query_type.upper()
        if qt == "UPDATE":
            content_type = 'application/sparql-update'
            accept = 'application/json'
        elif qt in ("CONSTRUCT", "DESCRIBE"):
            content_type = 'application/sparql-query'
            accept = 'text/turtle'
        else:
            content_type = 'application/sparql-query'
            accept = 'application/json'

        headers = {
            'Content-Type': content_type,
            'Accept': accept
        }

        # Get AWS credentials and sign request
        session = boto3.Session()
        credentials = session.get_credentials()

        request = AWSRequest(
            method='POST',
            url=sparql_url,
            data=sparql_query,
            headers=headers
        )

        SigV4Auth(credentials, 'neptune-db', region).add_auth(request)

        # Execute signed request
        response = requests.post(
            sparql_url,
            headers=dict(request.headers),
            data=sparql_query,
            timeout=30
        )

        if response.status_code == 200:
            if qt in ("CONSTRUCT", "DESCRIBE"):
                # RDF graph as Turtle. Wrap in JSON so the {statusCode, body}
                # envelope + the MCP string-result contract still hold; the
                # caller (Phase 3 slice builder) reads the "turtle" field and
                # parses it into an rdflib.Graph.
                return json.dumps({
                    "query_type": qt,
                    "turtle": response.text,
                })
            elif qt == "UPDATE":
                # For UPDATE queries, return success status
                return json.dumps({
                    "success": True,
                    "message": "SPARQL update executed successfully",
                    "query_type": query_type
                })
            else:
                # SELECT (and the default) → SPARQL-results JSON
                result = response.json()
                return json.dumps(result)
        else:
            return json.dumps({
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text}",
                "query_type": query_type
            })

    except Exception as e:
        logger.error(f"Error executing SPARQL query: {str(e)}")
        return json.dumps({
            "success": False,
            "error": str(e),
            "query_type": query_type
        })


def tool_get_graph_summary(ontology_id: str) -> str:
    """
    Get summary statistics for an ontology graph

    Args:
        ontology_id: Ontology identifier

    Returns:
        JSON string with summary statistics (class count, property count, triple count)
    """
    try:
        ontology_id = _validate_ontology_id(ontology_id)
        # Construct graph URI from ontology ID
        graph_uri = _resolve_graph_uri(ontology_id)

        # SPARQL query to count classes, properties (DatatypeProperty + ObjectProperty), and triples
        query = (f"""
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX owl: <http://www.w3.org/2002/07/owl#>

        SELECT
            (COUNT(DISTINCT ?class) AS ?classCount)
            (COUNT(DISTINCT ?property) AS ?propertyCount)
            (COUNT(*) AS ?tripleCount)
        FROM <{graph_uri}>
        WHERE {{
            {{
                ?class a owl:Class .
            }} UNION {{
                ?property a owl:DatatypeProperty .
            }} UNION {{
                ?property a owl:ObjectProperty .
            }} UNION {{
                ?s ?p ?o .
            }}
        }}
        """)  # nosec B608 - ontology_id validated by _validate_ontology_id; JWT-authenticated endpoint

        result = execute_sparql_query(query)

        bindings = result.get('results', {}).get('bindings', [])
        if not bindings:
            return json.dumps({
                'ontologyId': ontology_id,
                'graphUri': graph_uri,
                'classCount': 0,
                'propertyCount': 0,
                'tripleCount': 0
            })

        data = bindings[0]
        return json.dumps({
            'ontologyId': ontology_id,
            'graphUri': graph_uri,
            'classCount': int(data.get('classCount', {}).get('value', 0)),
            'propertyCount': int(data.get('propertyCount', {}).get('value', 0)),
            'tripleCount': int(data.get('tripleCount', {}).get('value', 0))
        })

    except Exception as e:
        logger.error(f"Error getting graph summary: {str(e)}")
        return json.dumps({
            'ontologyId': ontology_id,
            'error': str(e)
        })


def tool_get_graph_stats(ontology_id: str) -> str:
    """
    Get detailed statistics for an ontology graph (class distribution)

    Args:
        ontology_id: Ontology identifier

    Returns:
        JSON string with class distribution statistics (top 20 classes by instance count)
    """
    try:
        ontology_id = _validate_ontology_id(ontology_id)
        graph_uri = _resolve_graph_uri(ontology_id)

        # Query for class distribution
        query = (f"""
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX owl: <http://www.w3.org/2002/07/owl#>

        SELECT ?class (COUNT(*) AS ?instanceCount)
        FROM <{graph_uri}>
        WHERE {{
            ?instance a ?class .
            ?class a owl:Class .
        }}
        GROUP BY ?class
        ORDER BY DESC(?instanceCount)
        LIMIT 20
        """)  # nosec B608 - ontology_id validated by _validate_ontology_id; JWT-authenticated endpoint

        result = execute_sparql_query(query)

        bindings = result.get('results', {}).get('bindings', [])

        class_distribution = []
        for binding in bindings:
            class_distribution.append({
                'class': binding.get('class', {}).get('value', ''),
                'instanceCount': int(binding.get('instanceCount', {}).get('value', 0))
            })

        return json.dumps({
            'ontologyId': ontology_id,
            'graphUri': graph_uri,
            'classDistribution': class_distribution
        })

    except Exception as e:
        logger.error(f"Error getting graph stats: {str(e)}")
        return json.dumps({
            'ontologyId': ontology_id,
            'error': str(e)
        })


def tool_get_graph_classes(ontology_id: str) -> str:
    """
    Get list of all classes in the ontology

    Args:
        ontology_id: Ontology identifier

    Returns:
        JSON string with list of classes including URIs, labels, and comments
    """
    try:
        ontology_id = _validate_ontology_id(ontology_id)
        graph_uri = _resolve_graph_uri(ontology_id)

        query = (f"""
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX owl: <http://www.w3.org/2002/07/owl#>

        SELECT DISTINCT ?class ?label ?comment
        FROM <{graph_uri}>
        WHERE {{
            ?class a owl:Class .
            OPTIONAL {{ ?class rdfs:label ?label . }}
            OPTIONAL {{ ?class rdfs:comment ?comment . }}
        }}
        ORDER BY ?label
        """)  # nosec B608 - ontology_id validated by _validate_ontology_id; JWT-authenticated endpoint

        result = execute_sparql_query(query)

        bindings = result.get('results', {}).get('bindings', [])

        classes = []
        for binding in bindings:
            classes.append({
                'uri': binding.get('class', {}).get('value', ''),
                'label': binding.get('label', {}).get('value', ''),
                'comment': binding.get('comment', {}).get('value', '')
            })

        return json.dumps({
            'ontologyId': ontology_id,
            'classes': classes
        })

    except Exception as e:
        logger.error(f"Error getting graph classes: {str(e)}")
        return json.dumps({
            'ontologyId': ontology_id,
            'error': str(e)
        })


def tool_get_graph_properties(ontology_id: str) -> str:
    """
    Get all datatype properties and object-property relationships in the ontology.

    For a virtual knowledge graph the ontology is schema-only (TBox), so this
    queries owl:DatatypeProperty (column-level mappings) and owl:ObjectProperty
    (class-to-class relationships) separately and returns both lists.

    Args:
        ontology_id: Ontology identifier

    Returns:
        JSON string with:
          properties    – list of owl:DatatypeProperty entries
                          {uri, label, comment, domain, range, mapsToColumn, mapsToTable}
          relationships – list of owl:ObjectProperty entries
                          {uri, label, comment, from, to}
    """
    try:
        ontology_id = _validate_ontology_id(ontology_id)
        graph_uri = _resolve_graph_uri(ontology_id)

        # ── Datatype properties ────────────────────────────────────────────────
        datatype_query = (f"""
        PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX owl:  <http://www.w3.org/2002/07/owl#>
        PREFIX vkg:  <https://semantic-layer.aws/virtual-kg/>

        SELECT DISTINCT ?property ?label ?comment ?domain ?range ?mapsToColumn ?mapsToTable
        FROM <{graph_uri}>
        WHERE {{
            ?property a owl:DatatypeProperty .
            OPTIONAL {{ ?property rdfs:label      ?label        . }}
            OPTIONAL {{ ?property rdfs:comment    ?comment      . }}
            OPTIONAL {{ ?property rdfs:domain     ?domain       . }}
            OPTIONAL {{ ?property rdfs:range      ?range        . }}
            OPTIONAL {{ ?property vkg:mapsToColumn ?mapsToColumn . }}
            OPTIONAL {{ ?property vkg:mapsToTable  ?mapsToTable  . }}
        }}
        ORDER BY ?domain ?label
        """)  # nosec B608 - ontology_id validated by _validate_ontology_id; JWT-authenticated endpoint

        datatype_result = execute_sparql_query(datatype_query)
        properties = []
        for b in datatype_result.get('results', {}).get('bindings', []):
            properties.append({
                'uri':          b.get('property',     {}).get('value', ''),
                'label':        b.get('label',        {}).get('value', ''),
                'comment':      b.get('comment',      {}).get('value', ''),
                'domain':       b.get('domain',       {}).get('value', ''),
                'range':        b.get('range',        {}).get('value', ''),
                'mapsToColumn': b.get('mapsToColumn', {}).get('value', ''),
                'mapsToTable':  b.get('mapsToTable',  {}).get('value', ''),
            })

        # ── Object properties (relationships between classes) ──────────────────
        object_query = (f"""
        PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX owl:  <http://www.w3.org/2002/07/owl#>

        SELECT DISTINCT ?property ?label ?comment ?domain ?range
        FROM <{graph_uri}>
        WHERE {{
            ?property a owl:ObjectProperty .
            OPTIONAL {{ ?property rdfs:label   ?label   . }}
            OPTIONAL {{ ?property rdfs:comment ?comment . }}
            OPTIONAL {{ ?property rdfs:domain  ?domain  . }}
            OPTIONAL {{ ?property rdfs:range   ?range   . }}
        }}
        ORDER BY ?label
        """)  # nosec B608 - ontology_id validated by _validate_ontology_id; JWT-authenticated endpoint

        object_result = execute_sparql_query(object_query)
        relationships = []
        for b in object_result.get('results', {}).get('bindings', []):
            relationships.append({
                'uri':     b.get('property', {}).get('value', ''),
                'name':    b.get('label',    {}).get('value', '') or b.get('property', {}).get('value', '').split('/')[-1],
                'comment': b.get('comment',  {}).get('value', ''),
                'from':    b.get('domain',   {}).get('value', '').split('/')[-1],
                'to':      b.get('range',    {}).get('value', '').split('/')[-1],
            })

        return json.dumps({
            'ontologyId':    ontology_id,
            'properties':    properties,
            'relationships': relationships,
        })

    except Exception as e:
        logger.error(f"Error getting graph properties: {str(e)}")
        return json.dumps({
            'ontologyId':    ontology_id,
            'properties':    [],
            'relationships': [],
            'error':         str(e)
        })


def tool_persist_to_neptune(nquad_data: str) -> str:
    """Persist RDF n-quad data to Neptune"""
    try:
        neptune_endpoint = get_neptune_endpoint()
        region = os.environ.get('AWS_REGION', 'us-east-1')

        # Parse n-quads and group by named graph
        graph_batches = defaultdict(list)
        nquad_pattern = re.compile(
            r'^(<[^>]+>)\s+(<[^>]+>)\s+(<[^>]+>|"[^"]*"(?:\^\^<[^>]+>)?)\s+(<[^>]+>)\s*\.\s*$'
        )

        total_triples = 0
        for line in nquad_data.strip().split('\n'):
            line = line.strip()
            if not line:
                continue

            match = nquad_pattern.match(line)
            if match:
                subject, predicate, obj, graph = match.groups()
                triple = f"{subject} {predicate} {obj} ."
                graph_batches[graph].append(triple)
                total_triples += 1

        if not graph_batches:
            return json.dumps({
                "success": False,
                "message": "No valid n-quads found"
            })

        # Build SPARQL INSERT DATA query
        graph_blocks = []
        for graph_uri, triples in graph_batches.items():
            triples_text = '\n    '.join(triples)
            graph_block = f"  GRAPH {graph_uri} {{\n    {triples_text}\n  }}"
            graph_blocks.append(graph_block)

        sparql_query = f"INSERT DATA {{\n{chr(10).join(graph_blocks)}\n}}"

        # Execute INSERT
        if ':8182' not in neptune_endpoint:
            sparql_url = f"{neptune_endpoint}:8182/sparql"
        else:
            sparql_url = f"{neptune_endpoint}/sparql"

        headers = {
            'Content-Type': 'application/sparql-update',
            'Accept': 'application/json'
        }

        session = boto3.Session()
        credentials = session.get_credentials()

        request = AWSRequest(
            method='POST',
            url=sparql_url,
            data=sparql_query,
            headers=headers
        )

        SigV4Auth(credentials, 'neptune-db', region).add_auth(request)

        response = requests.post(
            sparql_url,
            headers=dict(request.headers),
            data=sparql_query,
            timeout=30
        )

        if response.status_code == 200:
            return json.dumps({
                "success": True,
                "message": f"Persisted {total_triples} triples to Neptune"
            })
        else:
            return json.dumps({
                "success": False,
                "message": f"HTTP {response.status_code}: {response.text}"
            })

    except Exception as e:
        logger.error(f"Error persisting to Neptune: {str(e)}")
        return json.dumps({
            "success": False,
            "message": str(e)
        })


def lambda_handler(event, context):
    """
    Lambda handler for Neptune tools via AgentCore Gateway

    Expected event format from Gateway:
    {
        "tool_name": "discover_named_graphs",
        "arguments": {...}
    }

    Available tools (9):
    - discover_named_graphs: List all named graphs
    - get_ontology_from_neptune: Retrieve ontology by ontology_id
    - persist_to_neptune: Write RDF data to Neptune
    - delete_graph: Drop all triples in a named graph by ontology_id
    - execute_sparql_query: Execute generic SPARQL queries
    - get_graph_summary: Get summary statistics (counts)
    - get_graph_stats: Get class distribution statistics
    - get_graph_classes: List all classes with metadata
    - get_graph_properties: List all properties with metadata

    How responses work:
    1. Gateway invokes Lambda via AWS Lambda API (synchronous)
    2. Lambda executes in VPC, accesses Neptune
    3. Lambda returns response object
    4. Response travels back through Lambda Service to Gateway
    5. No VPC routing needed for response path
    """
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        # Per AgentCore Gateway docs the tool name is in the Lambda context, not the event.
        # The event is a flat map of the tool arguments only.
        # context.client_context.custom['bedrockAgentCoreToolName'] = "<target-name>___<tool-name>"
        raw_tool_name = None
        if context and context.client_context and context.client_context.custom:
            raw_tool_name = context.client_context.custom.get('bedrockAgentCoreToolName')

        # Strip the target-name prefix: "discover-named-graphs___discover_named_graphs" -> "discover_named_graphs"
        if raw_tool_name and '___' in raw_tool_name:
            tool_name = raw_tool_name.split('___', 1)[1].replace('-', '_')
        elif raw_tool_name:
            tool_name = raw_tool_name.replace('-', '_')
        else:
            tool_name = None

        # The event IS the arguments – a flat dict of the tool's input properties.
        arguments = event

        logger.info(f"Tool name normalization: raw='{raw_tool_name}' -> normalized='{tool_name}'")

        # Route to appropriate tool
        if tool_name == 'discover_named_graphs':
            result = tool_discover_named_graphs()

        elif tool_name == 'get_ontology_from_neptune':
            ontology_id = arguments.get('ontology_id')
            if not ontology_id:
                return {
                    'statusCode': 400,
                    'body': json.dumps({"error": "ontology_id is required"})
                }
            result = tool_get_ontology_from_neptune(ontology_id)

        elif tool_name == 'persist_to_neptune':
            nquad_data = arguments.get('nquad_data')
            if not nquad_data:
                return {
                    'statusCode': 400,
                    'body': json.dumps({"error": "nquad_data is required"})
                }
            result = tool_persist_to_neptune(nquad_data)

        elif tool_name == 'delete_graph':
            ontology_id = arguments.get('ontology_id')
            if not ontology_id:
                return {
                    'statusCode': 400,
                    'body': json.dumps({"error": "ontology_id is required"})
                }
            result = tool_delete_graph(ontology_id)

        elif tool_name == 'execute_sparql_query':
            sparql_query = arguments.get('sparql_query')
            if not sparql_query:
                return {
                    'statusCode': 400,
                    'body': json.dumps({"error": "sparql_query is required"})
                }
            query_type = arguments.get('query_type', 'SELECT')
            result = tool_execute_sparql_query(sparql_query, query_type)

        elif tool_name == 'get_graph_summary':
            ontology_id = arguments.get('ontology_id')
            if not ontology_id:
                return {
                    'statusCode': 400,
                    'body': json.dumps({"error": "ontology_id is required"})
                }
            result = tool_get_graph_summary(ontology_id)

        elif tool_name == 'get_graph_stats':
            ontology_id = arguments.get('ontology_id')
            if not ontology_id:
                return {
                    'statusCode': 400,
                    'body': json.dumps({"error": "ontology_id is required"})
                }
            result = tool_get_graph_stats(ontology_id)

        elif tool_name == 'get_graph_classes':
            ontology_id = arguments.get('ontology_id')
            if not ontology_id:
                return {
                    'statusCode': 400,
                    'body': json.dumps({"error": "ontology_id is required"})
                }
            result = tool_get_graph_classes(ontology_id)

        elif tool_name == 'get_graph_properties':
            ontology_id = arguments.get('ontology_id')
            if not ontology_id:
                return {
                    'statusCode': 400,
                    'body': json.dumps({"error": "ontology_id is required"})
                }
            result = tool_get_graph_properties(ontology_id)

        else:
            return {
                'statusCode': 400,
                'body': json.dumps({"error": f"Unknown tool: {tool_name}"})
            }

        return {
            'statusCode': 200,
            'body': result
        }

    except Exception as e:
        logger.error(f"Lambda execution error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({"error": str(e)})
        }
