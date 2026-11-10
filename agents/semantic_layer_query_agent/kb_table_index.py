"""Topic-router recall source: the layer's KB documents in S3.

Phase 1 of the Hybrid workflow recalls candidate TABLES (``{database}.{table}``
ids), not ontology IRIs. That makes the KB markdown corpus the right hydration
source rather than the thin VKG:

* the thin VKG deliberately carries **no prose** (``vkg_builder`` skips every
  ``rdfs:comment``) and stores only the BARE table name in ``vkg:mapsToTable``,
  so it can neither produce a good recall embedding nor reconstruct the
  ``{database}.{table}`` id Phase 1 hands to the Athena execution context;
* the KB documents carry exactly both — the rich prose the creation agent
  composed, and the database/table/catalog identity encoded in their S3 key.

This module lists that corpus and returns one flat entry per table, shaped for
:func:`knn_hydration.hydrate_topic_router_namespace` so the per-namespace
``topic-router-<ns>`` index can be built lazily on the first query.

Isolation note: standard library + this package's modules only, with ``boto3``
kept behind an injected ``list_objects`` / ``get_object`` seam so importing this
module never touches AWS (``tests/unit/test_hybrid_isolation.py``).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

# Reuse the slice builder's markdown section splitter rather than re-implementing
# it: both parse the SAME documents, so a second regex here could drift out of
# step with the creation agent's document layout.
from .slice_builder import section as _md_section

logger = logging.getLogger(__name__)

# Sections whose prose best predicts "does this table answer the question?".
# The full document also carries Columns / Sample Data / Notes, which add many
# tokens of low-signal detail (and can push a long document past the embedding
# model's input limit), so recall text is restricted to these three.
RECALL_SECTIONS: tuple[str, ...] = (
    "Overview",
    "Business Purpose",
    "Business Concepts & Synonyms",
)

# Upper bound on characters fed to the embedding model per table. Titan v2
# truncates silently past its token limit; capping here keeps that truncation
# explicit and bounded rather than model-version dependent.
MAX_RECALL_CHARS: int = 6000

# The ``.md`` documents live beside a ``.md.metadata.json`` sidecar; the sidecar
# is the KB's chunk-metadata carrier and must never be treated as a document.
_DOC_SUFFIX = ".md"
_SIDECAR_SUFFIX = ".md.metadata.json"


def kb_prefix(*, layer_id: str, version: str) -> str:
    """Return the S3 key prefix holding a layer version's KB documents.

    Must stay in lockstep with
    ``semantic_layer_creation_agent.kb_builder.kb_object_key``.

    Parameters:
        layer_id: The semantic-layer id (DynamoDB partition key).
        version: The layer version sort-key (e.g. ``'v1'``).
    Returns:
        str: The key prefix, with a trailing slash.
    """
    return f"metadata/{layer_id}/{version}/"


def table_id_from_key(*, key: str, prefix: str) -> Optional[str]:
    """Derive the ``{database}.{table}`` candidate id from a KB document key.

    ``kb_object_key`` builds keys as
    ``metadata/{id}/{version}/{catalog}/{database}/{table}.md``, where the
    catalog segment itself may contain slashes (a federated catalog such as
    ``s3tablescatalog/semantic-layer-dev-analytics-tables``). Counting from the
    END is therefore the only stable parse: the table is the last segment and
    the database the one before it, whatever the catalog's depth.

    Deriving from the key rather than reading each ``.md.metadata.json`` sidecar
    is exact — the sidecar values are what BUILT this key — and avoids one extra
    S3 GET per table on the cold-start path.

    Parameters:
        key: The full S3 object key.
        prefix: The layer-version prefix the key was listed under.
    Returns:
        Optional[str]: The ``{database}.{table}`` id, or ``None`` when the key is
            not a KB document (e.g. a sidecar) or is too shallow to carry both a
            database and a table segment.
    """
    if not key.startswith(prefix):
        return None
    if key.endswith(_SIDECAR_SUFFIX) or not key.endswith(_DOC_SUFFIX):
        return None
    segments = [s for s in key[len(prefix):].split("/") if s]
    # Need at least {catalog}/{database}/{table}.md to name a database.
    if len(segments) < 3:
        return None
    database = segments[-2]
    table = segments[-1][: -len(_DOC_SUFFIX)]
    if not database or not table:
        return None
    return f"{database}.{table}"


def recall_text(*, markdown: str, table_id: str) -> str:
    """Build the embedding text for one table from its KB markdown.

    Leads with the table id so a question naming the table literally still
    matches when the prose does not, then appends the business-meaning sections
    listed in :data:`RECALL_SECTIONS`.

    Parameters:
        markdown: The full KB markdown document body.
        table_id: The ``{database}.{table}`` id, used as the leading token.
    Returns:
        str: The recall text, truncated to :data:`MAX_RECALL_CHARS`.
    """
    parts: List[str] = [table_id]
    for title in RECALL_SECTIONS:
        body = _md_section(md=markdown, title=title)
        if body:
            parts.append(body)
    return " | ".join(parts)[:MAX_RECALL_CHARS]


def build_table_metadata_fetcher(
    *,
    layer_id: str,
    version: str,
    bucket: str,
    list_keys: Callable[[str], List[str]],
    get_text: Callable[[str], str],
) -> Callable[[str], List[Dict[str, Any]]]:
    """Build the ``fetch_iri_metadata`` callable topic-router hydration expects.

    :func:`knn_hydration.hydrate_topic_router_namespace` indexes whatever the
    fetcher returns, keying each document on its ``iri`` field. For the Hybrid
    agent that key is a ``{database}.{table}`` candidate id rather than an
    ontology IRI — Phase 1 recalls tables, so the id it needs back from a KNN
    hit is the table id. The field name is retained so the same hydration
    routine serves both agents.

    Parameters:
        layer_id: The semantic-layer id whose KB corpus to read.
        version: The layer version whose KB corpus to read.
        bucket: The artifacts bucket holding the KB documents.
        list_keys: Callable ``list_keys(prefix) -> [key, ...]`` listing every
            object under a prefix (injected; keeps boto3 out of import).
        get_text: Callable ``get_text(key) -> str`` returning an object's decoded
            body (injected for the same reason).
    Returns:
        Callable: ``fetch(namespace) -> [{iri, label, comment, synonyms, kind}]``.
    Raises:
        RuntimeError: The layer's KB prefix holds no documents. A layer with no
            KB corpus can never recall a candidate, so every query would abstain
            for a reason unrelated to the question — fail loud instead.
    """
    def _fetch(namespace: str) -> List[Dict[str, Any]]:
        """Read the layer's KB corpus and return one entry per table.

        Parameters:
            namespace: Accepted for the hydration protocol's signature. The KB
                corpus is scoped by ``layer_id``/``version`` captured above,
                which is the authoritative scope (a layer's namespace need not
                equal its id), so this argument is intentionally unused.
        Returns:
            List[Dict[str, Any]]: One ``{iri, label, comment, synonyms, kind}``
                entry per KB document found.
        """
        prefix = kb_prefix(layer_id=layer_id, version=version)
        entries: List[Dict[str, Any]] = []
        for key in list_keys(prefix):
            table_id = table_id_from_key(key=key, prefix=prefix)
            if table_id is None:
                continue
            markdown = get_text(key)
            entries.append({
                "iri": table_id,
                "label": table_id,
                "comment": recall_text(markdown=markdown, table_id=table_id),
                "synonyms": [],
                "kind": "table",
            })
        if not entries:
            raise RuntimeError(
                f"No KB documents under s3://{bucket}/{prefix} for layer "
                f"{layer_id!r} (namespace {namespace!r}) — the topic-router "
                f"index would be empty and every question would fail to recall "
                f"a candidate table. Was the layer's KB phase ever built?"
            )
        logger.info(
            "kb_table_index: layer=%s version=%s tables=%d",
            layer_id, version, len(entries),
        )
        return entries

    return _fetch


def build_chunks_lookup(
    *,
    layer_id: str,
    version: str,
    list_keys: Callable[[str], List[str]],
    get_text: Callable[[str], str],
) -> Callable[..., Dict[str, str]]:
    """Build the ``chunks_lookup`` callable :class:`SliceBuilder` needs.

    The slice builder parses each candidate table's KB markdown for its Columns
    table, Referenced-By joins, ACORD path and query patterns. WITHOUT a chunk
    source it leaves ``columns``/``joins`` empty, so the Phase-3 sufficiency judge
    sees bare table names, reports the columns it needs as ``missing``,
    ``expand()`` finds nothing new to fold in, and the graph degrades
    ``phase3_max_rounds`` — claiming a table is unavailable while holding it.

    Returns the FULL document body, unlike :func:`recall_text`, which trims to the
    business-prose sections: the slice builder needs exactly the sections recall
    drops.

    The prefix listing is cached in the closure, so the S3 ``LIST`` happens once
    per built lookup rather than once per candidate batch (Phase 3 calls this on
    every build and every expand round).

    Parameters:
        layer_id: The semantic-layer id whose KB corpus to read.
        version: The layer version whose KB corpus to read.
        list_keys: Callable ``list_keys(prefix) -> [key, ...]`` (injected; keeps
            boto3 out of import).
        get_text: Callable ``get_text(key) -> str`` returning a decoded body.
    Returns:
        Callable: ``lookup(table_ids, namespace) -> {table_id: markdown}``. A
            requested id with no KB document is ABSENT from the result (never
            present-and-empty) — the slice builder narrows ``tables`` to the ids
            that produced a chunk, so a phantom id must not survive.
    """
    prefix = kb_prefix(layer_id=layer_id, version=version)
    # {table_id: s3_key}, built on first use. Cached because Phase 3 invokes the
    # lookup once per build and once per expand round.
    key_by_table: Dict[str, str] = {}

    def _ensure_key_map() -> None:
        """List the layer's KB prefix once and index the keys by table id."""
        if key_by_table:
            return
        for key in list_keys(prefix):
            table_id = table_id_from_key(key=key, prefix=prefix)
            if table_id is not None:
                key_by_table[table_id] = key
        logger.info(
            "kb_table_index.chunks: layer=%s version=%s documents=%d",
            layer_id, version, len(key_by_table),
        )

    def _lookup(*, table_ids: List[str], namespace: str) -> Dict[str, str]:
        """Return the KB markdown for each requested table that has one.

        Parameters:
            table_ids: Candidate ``{database}.{table}`` ids to fetch.
            namespace: Accepted for the slice-builder protocol's signature; the
                corpus is scoped by ``layer_id``/``version`` captured above, so
                this argument is intentionally unused.
        Returns:
            Dict[str, str]: ``{table_id: markdown}`` for the ids that resolved.
        """
        _ensure_key_map()
        out: Dict[str, str] = {}
        for table_id in table_ids or []:
            key = key_by_table.get(table_id)
            if key is None:
                # Not an error: the judge's ``missing`` list can name a table the
                # layer does not have, and the builder relies on absence here to
                # drop it rather than treat it as a real join target.
                continue
            out[table_id] = get_text(key)
        return out

    return _lookup


def build_s3_seams(
    *, bucket: str, s3_client: Any,
) -> tuple[Callable[[str], List[str]], Callable[[str], str]]:
    """Build the ``(list_keys, get_text)`` S3 seams over a boto3 client.

    Paginates the listing manually rather than via a paginator so this works
    with any client-shaped stub in tests.

    Parameters:
        bucket: The bucket to read.
        s3_client: A boto3 S3 client (or a compatible stub).
    Returns:
        tuple: ``(list_keys, get_text)`` callables.
    """
    def _list_keys(prefix: str) -> List[str]:
        """Return every object key under ``prefix``, following continuations."""
        keys: List[str] = []
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = s3_client.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in resp.get("Contents", []) or [])
            if not resp.get("IsTruncated"):
                return keys
            token = resp.get("NextContinuationToken")
            if not token:
                return keys

    def _get_text(key: str) -> str:
        """Return one object's body decoded as UTF-8."""
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read().decode("utf-8")

    return _list_keys, _get_text
