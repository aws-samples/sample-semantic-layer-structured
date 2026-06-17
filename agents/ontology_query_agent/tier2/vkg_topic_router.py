"""Phase 1 (VKG mode): KNN over the per-namespace ``topic-router-<ns>`` index.

The in-memory KNN store is hydrated lazily on first call from Neptune
class/property metadata via :func:`agents.shared.knn_hydration.hydrate_topic_router_namespace`.
Falls back to a lexical SPARQL substring match if hydration fails so the
query path never 500s on a brand-new namespace.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

try:
    from agents.shared import knn_hydration
    from agents.shared.knn_index import IndexNotFoundError
except ImportError:  # container path: agents/ is on PYTHONPATH
    from shared import knn_hydration  # type: ignore
    from shared.knn_index import IndexNotFoundError  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_K: int = 20
"""Default top-K for the namespace-scoped KNN search."""


class VkgTopicRouter:
    """Phase 1 candidate-IRI router for VKG mode.

    Exposes ``find_candidates(question, namespace) -> list[str]``, the router
    contract the Tier 2 graph workflow's Phase 1 node calls into.
    """

    def __init__(self, *, endpoint: str, knn,
                 embed_fn: Callable[[str], List[float]],
                 neptune_lexical: Any, k: int = DEFAULT_K,
                 fetch_iri_metadata: Optional[
                     Callable[[str], List[dict]]
                 ] = None) -> None:
        """Initialize the router.

        Args:
            endpoint: Unused — kept for backwards compatibility with the
                OSS-era constructor signature. Pass ``""``.
            knn: ``agents.shared.knn_index`` module (or stand-in).
            embed_fn: Callable that turns a question string into a vector.
            neptune_lexical: Object exposing
                ``lexical_match(question, namespace) -> list[str]`` for the
                cold-start fallback.
            k: Top-K for the KNN search.
            fetch_iri_metadata: Callable to populate the in-memory KNN
                index from Neptune on cold start. Defaults to
                ``agents.shared.neptune_metadata.get_ontology_metadata``.
                Tests inject a stub.
        """
        self.endpoint = endpoint
        self.knn = knn
        self.embed = embed_fn
        self.lex = neptune_lexical
        self.k = k
        self.last_degraded: Optional[str] = None
        if fetch_iri_metadata is None:
            try:
                from agents.shared.neptune_metadata import get_ontology_metadata
            except ImportError:  # container path: agents/ is on PYTHONPATH
                from shared.neptune_metadata import get_ontology_metadata  # type: ignore
            fetch_iri_metadata = get_ontology_metadata
        self._fetch_iri_metadata = fetch_iri_metadata

    def find_candidates(self, *, question: str, namespace: str) -> List[str]:
        """Return Phase 1 candidate IRIs for ``question`` in ``namespace``.

        Falls back to lexical match if the KNN index is missing, recording
        ``self.last_degraded = "phase1_cold_start"`` so the orchestrator can
        annotate the result.
        """
        self.last_degraded = None
        index = f"topic-router-{namespace}"

        # Lazy hydration: build the in-memory index from Neptune on first
        # call for this namespace. Idempotent — subsequent calls are no-ops.
        try:
            knn_hydration.hydrate_topic_router_namespace(
                namespace=namespace,
                fetch_iri_metadata=self._fetch_iri_metadata,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "phase1.hydration_failed namespace=%s err=%s — falling back to lexical",
                namespace, e,
            )
            self.last_degraded = "phase1_cold_start"
            return list(
                self.lex.lexical_match(question=question, namespace=namespace)
            )

        try:
            hits = self.knn.knn_search(
                endpoint=self.endpoint, index=index,
                vector=self.embed(question), k=self.k,
            )
            return [h["id"] for h in hits]
        except IndexNotFoundError:
            logger.warning(
                "phase1.degraded=cold_start namespace=%s", namespace,
            )
            self.last_degraded = "phase1_cold_start"
            return list(
                self.lex.lexical_match(question=question, namespace=namespace)
            )
