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
from typing import Dict, Any, List
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Reduce AWS SDK noise
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)


# Grammar accepted by tool_persist_to_neptune, one quad per physical line:
#   <subject> <predicate> (<iri>|"literal"[^^<datatype>|@lang]) <graph> .
#
# The literal alternative MUST allow backslash escapes — a description that
# contains a double quote or a newline is escaped by the producer as \" / \n and
# is perfectly legal N-Quads. An earlier `"[^"]*"` rejected every such line, and
# because unmatched lines were skipped silently, descriptions quoting anything
# (e.g. sets "expansions" or "editions") vanished from the graph. The language-tag
# suffix is accepted for parity with the frontend parser in
# frontend/src/components/OntologyEditor.jsx, which reads these same lines back.
#
# The IRI class is SPARQL's IRIREF production, NOT the laxer `[^>]+` that N-Quads
# alone permits. Every line that matches is interpolated into an INSERT DATA
# query, so accepting an IRI SPARQL cannot lex just moves the failure downstream:
# a column named "academic year" produced <.../frpm/academic year>, which the old
# pattern accepted and Neptune then rejected with a MalformedQueryException whose
# column number pointed at the vocabulary host, naming neither the line nor the
# column. Rejecting it here reports the offending line instead.
#
# MUST stay identical to _NQUAD_PATTERN in
# agents/semantic_layer_creation_agent/neptune_load.py, which pre-validates against
# this grammar before sending.
IRIREF = r'<[^\x00-\x20<>"{}|^`\\]+>'
NQUAD_PATTERN = re.compile(
    rf'^({IRIREF})\s+({IRIREF})\s+'
    rf'({IRIREF}|"(?:[^"\\]|\\.)*"(?:\^\^{IRIREF}|@[A-Za-z][A-Za-z0-9-]*)?)'
    rf'\s+({IRIREF})\s*\.\s*$'
)


def _validate_ontology_id(ontology_id: str) -> str:
    """Validate ontology_id is safe for use in SPARQL query construction."""
    if not re.match(r'^[a-zA-Z0-9_\-]+$', str(ontology_id)):
        raise ValueError(f"Invalid ontology_id format: {ontology_id!r}")
    return ontology_id


# Characters that would let a database/table name break OUT of the
# ``"<db>.<table>"`` SPARQL string literal (or the surrounding WHERE clause) in
# tool_delete_table_triples and rewrite the query to delete the whole graph.
# A double-quote closes the literal; backslash starts an escape; braces and
# newlines restructure the clause. Everything else — including hyphens and dots,
# which DynamoDB-connector table names legitimately use (e.g.
# ``semantic-layer-admin-codes``) — is SAFE inside a double-quoted literal.
_SPARQL_LITERAL_BREAKERS = re.compile(r'["\\{}\r\n]')


def _validate_sql_identifier(value: str, *, field: str) -> str:
    """Validate a database/table identifier before interpolating it into a SPARQL
    string literal.

    Rejects only the characters that can break out of the ``"<db>.<table>"`` literal
    (``"``, ``\\``, ``{``, ``}``, CR, LF). A previous allowlist of ``[A-Za-z0-9_]``
    was too strict — it rejected legitimate hyphenated DynamoDB-connector table names
    (e.g. ``semantic-layer-admin-codes``), silently leaving their triples in Neptune
    on removal. Hyphens/dots are harmless inside a double-quoted SPARQL literal.

    Args:
        value: The identifier to validate (database or table name).
        field: Human-readable field name for the error message.

    Returns:
        The validated identifier unchanged.

    Raises:
        ValueError: if the identifier is empty or contains a literal-breaking char.
    """
    if not value or _SPARQL_LITERAL_BREAKERS.search(str(value)):
        raise ValueError(f"Invalid {field} for SPARQL interpolation: {value!r}")
    return value


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


def _resolve_graph_uris(ontology_id: str) -> List[str]:
    """Discover EVERY named-graph URI for an ontology_id, newest first.

    The agent stores triples under a graph URI of the form
    ``http://{slug-of-ontology-name}/ontology/{ontology_id}``, and the name
    carries a build timestamp (``bird-bird_card_games-ontology-20260806-084812``).
    So a REBUILD of the same layer id lands in a DIFFERENT graph and the old one
    survives — as of 2026-08-09 every rebuilt BIRD layer had 3 graphs in Neptune.
    Rather than looking up the name from DynamoDB, we query Neptune for any
    named graph whose URI contains the UUID — this works for any name.

    Sorting by the URI descending puts the newest build first, because the
    embedded timestamp is fixed-width ``YYYYMMDD-HHMMSS`` and therefore sorts
    lexicographically in chronological order. Without an explicit order Neptune
    may return any of them, which silently serves a STALE ontology: the
    2026-08-06 rebuild of every BIRD layer was invisible because resolution kept
    returning the 2026-08-04 graph.

    Governed rules live in a SIBLING graph ``<ontology graph>/rules`` whose
    URI also contains the UUID. It is excluded both in SPARQL and again in
    Python — returning it here would make ontology reads silently resolve to
    rule triples.

    Args:
        ontology_id: The layer id embedded in the graph URI.

    Returns:
        All matching ontology graph URIs, newest build first, never including a
        ``/rules`` graph. Empty if none exist.
    """
    sparql_query = f"""
    SELECT DISTINCT ?g
    WHERE {{
        GRAPH ?g {{ ?s ?p ?o }}
        FILTER(CONTAINS(STR(?g), "{ontology_id}"))
        FILTER(!STRENDS(STR(?g), "/rules"))
    }}
    ORDER BY DESC(?g)
    """
    try:
        result = execute_sparql_query(sparql_query)
        bindings = result.get('results', {}).get('bindings', [])
        # Defence in depth: never hand back a rules graph even if the FILTER
        # above is ever edited away. Order from the SPARQL ORDER BY is kept, so
        # the newest build stays first.
        return [
            b['g']['value']
            for b in bindings
            if not b['g']['value'].endswith('/rules')
        ]
    except Exception as e:
        logger.warning(f"Could not discover graph URIs for {ontology_id}: {e}")
        return []


def _resolve_graph_uri(ontology_id: str) -> str:
    """Return the NEWEST named-graph URI for an ontology_id.

    Args:
        ontology_id: The layer id embedded in the graph URI.

    Returns:
        The most recently built graph's URI.

    Raises:
        ValueError: No graph exists for this ontology_id.
    """
    uris = _resolve_graph_uris(ontology_id)
    if not uris:
        raise ValueError(
            f"No named graph found in Neptune for ontology_id '{ontology_id}'"
        )
    if len(uris) > 1:
        # Loud, because a stale graph silently serving an old ontology is exactly
        # the failure this ordering fixes; the count tells an operator to clean up.
        logger.warning(
            f"{len(uris)} named graphs exist for '{ontology_id}' (superseded "
            f"rebuilds were never dropped) — serving the newest, {uris[0]}. "
            f"Older: {uris[1:]}"
        )
    return uris[0]


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

                # rdfs:range — for an owl:ObjectProperty this is the TARGET class IRI
                # of an FK relationship (e.g. Coverage/hasHolding range = .../Holding).
                # The OBDA mapping generator needs it to emit the FK object as an IRI
                # template matching the target class's subject template (so Ontop can
                # reformulate the join), instead of a bare literal that never joins.
                # Datatype properties also have a range (the xsd type); harmless to
                # capture — the OBDA generator only consults range for ObjectProperty.
                elif predicate == 'http://www.w3.org/2000/01/rdf-schema#range':
                    if subject in ontology_info['properties']:
                        ontology_info['properties'][subject]['range'] = obj
                    else:
                        ontology_info['properties'].setdefault(subject, {})['range'] = obj

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
                    # setdefault-then-assign, NOT a fresh dict: hasPrimaryKey may
                    # arrive for this same class BEFORE mapsToTable (both are
                    # class-subject predicates and 'hasPrimaryKey' sorts before
                    # 'mapsToTable' under ORDER BY ?predicate), and overwriting
                    # would silently drop the verified key.
                    ontology_info['mappings'].setdefault(subject, {})['table'] = obj
                # Verified primary key for a class, stamped at build time by a
                # uniqueness probe. The translator prefers this over its column-name
                # heuristic for the OBDA subject template (= row identity) and only
                # emits a uniqueConstraint when it is present.
                elif 'hasPrimaryKey' in predicate:
                    ontology_info['mappings'].setdefault(subject, {})['primaryKey'] = obj
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


def _sparql_update(update_query: str) -> "requests.Response":
    """POST a SPARQL Update (DROP/DELETE) to the Neptune SPARQL endpoint with
    SigV4 auth. Single write path shared by delete_graph + delete_table_triples.

    Args:
        update_query: A SPARQL update string (DROP GRAPH / DELETE ...).

    Returns:
        The requests.Response from the Neptune SPARQL endpoint.
    """
    neptune_endpoint = get_neptune_endpoint()
    region = os.environ.get('AWS_REGION', 'us-east-1')
    sparql_url = (f"{neptune_endpoint}:8182/sparql" if ':8182' not in neptune_endpoint
                  else f"{neptune_endpoint}/sparql")
    headers = {'Content-Type': 'application/sparql-update', 'Accept': 'application/json'}
    credentials = boto3.Session().get_credentials()
    request = AWSRequest(method='POST', url=sparql_url, data=update_query, headers=headers)
    SigV4Auth(credentials, 'neptune-db', region).add_auth(request)
    return requests.post(sparql_url, headers=dict(request.headers),
                         data=update_query, timeout=30)


def tool_delete_graph(ontology_id: str) -> str:
    """Drop EVERY named graph belonging to an ontology_id.

    Deliberately drops all matches rather than only the newest: a rebuild mints a
    new graph URI (the name carries a build timestamp) and nothing used to remove
    the previous one, so layers accumulated superseded graphs that resolution could
    pick up. Deleting one at a time would leave a layer looking half-deleted.

    Args:
        ontology_id: The layer/ontology id embedded in the graph URIs.

    Returns:
        JSON string {success, ontology_id, graphs_deleted, triples_deleted, ...}.
    """
    try:
        ontology_id = _validate_ontology_id(ontology_id)
        graph_uris = _resolve_graph_uris(ontology_id)

        if not graph_uris:
            return json.dumps({
                "success": True,
                "ontology_id": ontology_id,
                "graphs_deleted": 0,
                "triples_deleted": 0,
                "message": f"No named graph exists for '{ontology_id}' — nothing to delete",
            })

        deleted: List[str] = []
        failures: List[str] = []
        triples_deleted = 0

        for graph_uri in graph_uris:
            # Count triples before deletion so we can report how many were removed
            count_query = (f"""
            SELECT (COUNT(*) AS ?tripleCount)
            FROM <{graph_uri}>
            WHERE {{ ?s ?p ?o . }}
            """)  # nosec B608 - ontology_id validated by _validate_ontology_id; JWT-authenticated endpoint
            count_result = execute_sparql_query(count_query)
            bindings = count_result.get('results', {}).get('bindings', [])
            triple_count = (int(bindings[0].get('tripleCount', {}).get('value', 0))
                            if bindings else 0)

            # Execute DROP GRAPH via the shared SigV4 SPARQL Update write path
            drop_query = f"DROP SILENT GRAPH <{graph_uri}>"  # nosec B608 - ontology_id validated by _validate_ontology_id; JWT-authenticated endpoint
            response = _sparql_update(drop_query)

            if response.status_code == 200:
                deleted.append(graph_uri)
                triples_deleted += triple_count
            else:
                failures.append(f"{graph_uri}: HTTP {response.status_code} {response.text}")

        if failures:
            # Partial deletion is a failure: a surviving graph can still be served.
            return json.dumps({
                "success": False,
                "ontology_id": ontology_id,
                "graphs_deleted": deleted,
                "triples_deleted": triples_deleted,
                "message": f"Failed to drop {len(failures)} graph(s): {'; '.join(failures)}",
            })

        return json.dumps({
            "success": True,
            "ontology_id": ontology_id,
            "graph_uri": deleted[0],  # retained for callers that read a single URI
            "graphs_deleted": deleted,
            "triples_deleted": triples_deleted,
            "message": (f"Deleted {triples_deleted} triples from {len(deleted)} "
                        f"graph(s) for '{ontology_id}'"),
        })

    except Exception as e:
        logger.error(f"Error deleting graph: {str(e)}")
        return json.dumps({"success": False, "error": str(e)})


def tool_delete_table_triples(ontology_id: str, database_name: str,
                              table_name: str) -> str:
    """Delete only the triples for one table within a layer's named graph.

    Scopes on the vkg:mapsToTable literal "<database>.<table>" so a removed table's
    class, its properties, and FK edges are dropped without touching sibling tables.

    Args:
        ontology_id: The layer/ontology id (validated; selects the named graph).
        database_name: The table's database (part of the mapsToTable literal).
        table_name: The table name (part of the mapsToTable literal).

    Returns:
        JSON string {success, ontology_id, table_name} or {success:false, ...}.
    """
    try:
        ontology_id = _validate_ontology_id(ontology_id)
        # Validate db/table BEFORE interpolation — a quote/brace/newline in either
        # would break out of the "<db>.<table>" literal and could rewrite the WHERE
        # clause to delete the whole graph (C2). Glue names that are Athena-queryable
        # pass this. Validation is inside the try so a bad identifier returns
        # {success: false} (fail-soft), matching how cleanup_removed treats it.
        database_name = _validate_sql_identifier(database_name, field="database_name")
        table_name = _validate_sql_identifier(table_name, field="table_name")
        graph_uri = _resolve_graph_uri(ontology_id)
        maps_to = f"{database_name}.{table_name}"          # real object literal form
        pred = f"<{VIRTUAL_KG_VOCAB}mapsToTable>"
        # Delete the class node that maps to this table, plus any subject whose
        # predicate/object references that class (its properties + FK ObjectProperties).
        sparql = f"""
        WITH <{graph_uri}>
        DELETE {{ ?s ?p ?o }}
        WHERE {{
          ?s ?p ?o .
          {{ ?s {pred} "{maps_to}" . }}
          UNION
          {{ ?cls {pred} "{maps_to}" . ?s ?anyp ?cls . }}
        }}
        """  # nosec B608 - ontology_id + db/table validated above
        resp = _sparql_update(sparql)
        if resp.status_code == 200:
            return json.dumps({"success": True, "ontology_id": ontology_id,
                               "table_name": table_name})
        return json.dumps({"success": False,
                           "message": f"HTTP {resp.status_code}: {resp.text}"})
    except Exception as e:  # noqa: BLE001
        logger.warning("delete_table_triples failed for %s.%s: %s",
                       ontology_id, table_name, e)
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

        total_triples = 0
        rejected = []
        for line in nquad_data.strip().split('\n'):
            line = line.strip()
            if not line:
                continue

            match = NQUAD_PATTERN.match(line)
            if match:
                subject, predicate, obj, graph = match.groups()
                triple = f"{subject} {predicate} {obj} ."
                graph_batches[graph].append(triple)
                total_triples += 1
            else:
                rejected.append(line)

        # Fail loudly instead of inserting the subset that parsed: a partially
        # materialized graph reported as success is indistinguishable from a clean
        # load, and callers (e.g. the Hybrid creation agent) treat success as
        # "everything I sent is now in Neptune".
        if rejected:
            return json.dumps({
                "success": False,
                "message": (
                    f"{len(rejected)} of {len(rejected) + total_triples} n-quads lines "
                    f"are not valid N-Quads; nothing was persisted. "
                    f"First offender: {rejected[0][:200]!r}"
                )
            })

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

        elif tool_name == 'delete_table_triples':
            ontology_id = arguments.get('ontology_id')
            database_name = arguments.get('database_name')
            table_name = arguments.get('table_name')
            if not ontology_id or not database_name or not table_name:
                return {
                    'statusCode': 400,
                    'body': json.dumps({"error": "ontology_id, database_name, table_name are required"})
                }
            result = tool_delete_table_triples(ontology_id, database_name, table_name)

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
