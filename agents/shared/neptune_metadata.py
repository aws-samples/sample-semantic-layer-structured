"""Thin Neptune SPARQL client used by topic-router hydration.

Reads class/property metadata (``rdfs:label``, ``rdfs:comment``) for a
namespace and returns flat dicts ready for embedding. The query agent's
runtime invokes this on first ``find_candidates`` call per namespace so
the in-memory ``topic-router-<ns>`` index is populated lazily.

Originally lived in ``lambda/topic-router-rebuild/neptune_tools_client.py``
when the rebuild was an EventBridge subscriber; moved here so the agent
runtime can hydrate without the OSS-era rebuild Lambda.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
from typing import Any, Dict, List
from urllib import request as _urlreq

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger(__name__)


def _require_https(url: str) -> str:
    """Raise ValueError if *url* does not use the https scheme.

    Prevents mis-configured env vars (http:// or file://) from reaching urllib.

    :param url: the URL to validate.
    :returns: the original url, unchanged.
    :raises ValueError: if the scheme is not https.
    """
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError(f"Refusing non-HTTPS URL: {url!r}")
    return url


def _neptune_endpoint() -> str:
    """Read NEPTUNE_ENDPOINT lazily so import-time tests don't require it."""
    endpoint = os.environ.get("NEPTUNE_ENDPOINT", "")
    if not endpoint:
        raise RuntimeError("NEPTUNE_ENDPOINT not configured")
    return _require_https(endpoint)


def _graph_uri(namespace: str) -> str:
    """Build the named-graph URI for ``namespace`` using GRAPH_URI_PREFIX."""
    prefix = os.environ.get(
        "GRAPH_URI_PREFIX", "https://semantic-layer.aws/ontologies/",
    )
    return f"{prefix}{namespace}"


def _execute_sparql(query: str) -> Dict[str, Any]:
    """POST a SPARQL SELECT against Neptune with SigV4 and return JSON."""
    url = f"{_neptune_endpoint()}/sparql"
    body = f"query={_urlreq.quote(query)}".encode()
    req = AWSRequest(
        method="POST", url=url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    SigV4Auth(
        boto3.Session().get_credentials(), "neptune-db",
        os.environ.get("AWS_REGION", "us-east-1"),
    ).add_auth(req)
    http_req = _urlreq.Request(
        url, data=body, headers=dict(req.headers.items()), method="POST",
    )
    with _urlreq.urlopen(http_req, timeout=30) as resp:  # nosec B310 — SigV4-signed; scheme enforced by _require_https in _neptune_endpoint()  # nosemgrep: dynamic-urllib-use-detected — fixed AWS service endpoint from config, not user-controlled host
        return json.loads(resp.read())


def get_ontology_metadata(namespace: str) -> List[Dict[str, Any]]:
    """Return ``{iri, label, comment, synonyms, kind}`` rows for ``namespace``.

    Issues two SELECTs (classes + properties) against the namespace's named
    graph and merges them. ``synonyms`` is left empty pending a synonym
    vocabulary in the ontology.
    """
    graph_uri = _graph_uri(namespace)

    classes_q = f"""
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX owl:  <http://www.w3.org/2002/07/owl#>
    SELECT DISTINCT ?iri ?label ?comment FROM <{graph_uri}> WHERE {{
        ?iri a owl:Class .
        OPTIONAL {{ ?iri rdfs:label   ?label .   }}
        OPTIONAL {{ ?iri rdfs:comment ?comment . }}
    }}
    """  # nosec B608 - graph_uri derived from controlled prefix + namespace

    props_q = f"""
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX owl:  <http://www.w3.org/2002/07/owl#>
    SELECT DISTINCT ?iri ?label ?comment FROM <{graph_uri}> WHERE {{
        {{ ?iri a owl:DatatypeProperty }} UNION {{ ?iri a owl:ObjectProperty }}
        OPTIONAL {{ ?iri rdfs:label   ?label .   }}
        OPTIONAL {{ ?iri rdfs:comment ?comment . }}
    }}
    """  # nosec B608 - graph_uri derived from controlled prefix + namespace

    out: List[Dict[str, Any]] = []
    for kind, q in (("class", classes_q), ("property", props_q)):
        result = _execute_sparql(q)
        for b in result.get("results", {}).get("bindings", []):
            iri = b.get("iri", {}).get("value", "")
            if not iri:
                continue
            out.append({
                "iri": iri,
                "label": b.get("label", {}).get("value", ""),
                "comment": b.get("comment", {}).get("value", ""),
                "synonyms": [],
                "kind": kind,
            })
    return out
