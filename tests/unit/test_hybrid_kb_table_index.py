"""Topic-router hydration source: the layer's KB corpus in S3.

Covers ``agents.semantic_layer_query_agent.kb_table_index`` plus the
hydrate-on-cold-start wiring in ``main._build_topic_router``. The gap these pin
shipped to AWS: the router searched a per-container in-memory KNN index that
nothing ever built, so EVERY hybrid query died with
``IndexNotFoundError: index not found: topic-router-hybrid-curated_layer``.
"""
import types

import pytest

from agents.semantic_layer_query_agent import (
    kb_table_index,
    knn_hydration,
    knn_index,
    main,
)

PREFIX = "metadata/hybrid-curated_layer/v1/"

# A federated catalog segment contains a slash, which is exactly why the table id
# must be parsed from the END of the key rather than by a fixed segment offset.
FEDERATED_KEY = (
    PREFIX + "s3tablescatalog/semantic-layer-dev-analytics-tables/"
    "normalized/address.md"
)

DOC = """# catalog.normalized.address

## Overview
One row per mailing address for a party.

## Business Purpose
Answers where a policyholder lives.

## Columns
| Column | Type | Description |
|---|---|---|
| party_id | string | Owning party. |

## Sample Data
Nothing that should reach the recall text.
"""


@pytest.fixture(autouse=True)
def _reset():
    """Clear both in-memory index state and hydration bookkeeping per test."""
    knn_index.reset_for_tests()
    knn_hydration.reset_for_tests()


# ---------------------------------------------------------------------------
# Key -> candidate-id parsing.
# ---------------------------------------------------------------------------
def test_table_id_parsed_from_federated_catalog_key():
    """A multi-segment catalog must still yield ``{database}.{table}``."""
    assert kb_table_index.table_id_from_key(
        key=FEDERATED_KEY, prefix=PREFIX,
    ) == "normalized.address"


def test_table_id_parsed_from_single_segment_catalog_key():
    """The common single-segment catalog case parses the same way."""
    assert kb_table_index.table_id_from_key(
        key=PREFIX + "AwsDataCatalog/sales/orders.md", prefix=PREFIX,
    ) == "sales.orders"


def test_sidecar_and_foreign_keys_are_skipped():
    """Sidecars, non-markdown keys and too-shallow keys yield no table id."""
    for key in (
        PREFIX + "AwsDataCatalog/sales/orders.md.metadata.json",
        PREFIX + "AwsDataCatalog/sales/orders.json",
        PREFIX + "orders.md",  # no database segment
        "metadata/other-layer/v1/AwsDataCatalog/sales/orders.md",
    ):
        assert kb_table_index.table_id_from_key(key=key, prefix=PREFIX) is None, key


# ---------------------------------------------------------------------------
# Recall text.
# ---------------------------------------------------------------------------
def test_recall_text_leads_with_id_and_omits_bulk_sections():
    """Recall text carries the id + business prose, not Columns / Sample Data."""
    text = kb_table_index.recall_text(markdown=DOC, table_id="normalized.address")

    assert text.startswith("normalized.address")
    assert "policyholder lives" in text
    assert "Nothing that should reach" not in text, "Sample Data must be excluded"
    assert "party_id" not in text, "Columns must be excluded"


def test_recall_text_is_truncated():
    """A pathologically long document cannot blow the embedding input limit."""
    huge = "# t\n\n## Overview\n" + ("x" * (kb_table_index.MAX_RECALL_CHARS * 3))

    text = kb_table_index.recall_text(markdown=huge, table_id="db.t")

    assert len(text) == kb_table_index.MAX_RECALL_CHARS


# ---------------------------------------------------------------------------
# The fetcher.
# ---------------------------------------------------------------------------
def _fetcher(keys, *, bodies=None):
    """Build a fetcher over in-memory keys/bodies.

    Args:
        keys: The object keys the fake listing returns.
        bodies: Optional key -> markdown map; defaults every key to ``DOC``.

    Returns:
        The ``fetch(namespace)`` callable under test.
    """
    bodies = bodies or {}
    return kb_table_index.build_table_metadata_fetcher(
        layer_id="hybrid-curated_layer", version="v1", bucket="b",
        list_keys=lambda prefix: keys,
        get_text=lambda key: bodies.get(key, DOC),
    )


def test_fetcher_returns_one_entry_per_document_keyed_by_table_id():
    """Entries are keyed on the candidate id Phase 1 consumes, not an IRI."""
    entries = _fetcher([
        FEDERATED_KEY,
        FEDERATED_KEY + ".metadata.json",
        PREFIX + "s3tablescatalog/bucket/normalized/party.md",
    ])("ns")

    assert [e["iri"] for e in entries] == ["normalized.address", "normalized.party"]
    assert all(e["kind"] == "table" for e in entries)
    assert "policyholder lives" in entries[0]["comment"]


def test_fetcher_fails_loud_on_empty_corpus():
    """No KB documents must raise, not yield a silently empty index."""
    with pytest.raises(RuntimeError, match="No KB documents"):
        _fetcher([])("ns")


def test_s3_seams_follow_pagination():
    """``list_keys`` must page — a 40-table layer exceeds no page, but 1000+ would."""
    pages = [
        {"Contents": [{"Key": "a"}], "IsTruncated": True,
         "NextContinuationToken": "t1"},
        {"Contents": [{"Key": "b"}], "IsTruncated": False},
    ]
    calls = []

    class _S3:
        def list_objects_v2(self, **kwargs):
            calls.append(kwargs.get("ContinuationToken"))
            return pages[len(calls) - 1]

    list_keys, _ = kb_table_index.build_s3_seams(bucket="b", s3_client=_S3())

    assert list_keys("p/") == ["a", "b"]
    assert calls == [None, "t1"]


# ---------------------------------------------------------------------------
# The wiring: cold start must hydrate rather than raise IndexNotFoundError.
# ---------------------------------------------------------------------------
class _FakeBody:
    """Minimal stand-in for a boto3 ``StreamingBody``."""

    def __init__(self, text: str) -> None:
        """Store the body text to be returned as bytes."""
        self._text = text

    def read(self) -> bytes:
        """Return the body encoded as UTF-8, as boto3 does."""
        return self._text.encode("utf-8")


class _FakeS3:
    """In-memory S3 holding one KB document per key."""

    def __init__(self, objects: dict) -> None:
        """Store the ``{key: markdown}`` corpus this fake serves."""
        self._objects = objects

    def list_objects_v2(self, **kwargs) -> dict:
        """Return every stored key under the requested prefix, unpaginated."""
        prefix = kwargs["Prefix"]
        return {
            "Contents": [
                {"Key": k} for k in sorted(self._objects) if k.startswith(prefix)
            ],
            "IsTruncated": False,
        }

    def get_object(self, **kwargs) -> dict:
        """Return one stored object's body."""
        return {"Body": _FakeBody(self._objects[kwargs["Key"]])}


def _stub_aws(monkeypatch, *, objects):
    """Wire the real hydration path over an in-memory S3 and embedder.

    Patches only the two AWS boundaries — the boto3 session and the Bedrock
    embedding call — so the test exercises ``_hydrate_topic_router``,
    ``kb_table_index`` and ``knn_hydration`` for real.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        objects: The ``{s3_key: markdown}`` corpus to serve.

    Returns:
        None.
    """
    monkeypatch.setenv("ARTIFACTS_BUCKET", "bucket")
    fake_s3 = _FakeS3(objects)
    monkeypatch.setattr(
        main, "get_boto_session",
        lambda: types.SimpleNamespace(client=lambda *a, **kw: fake_s3),
    )
    # Deterministic embedder: vary the leading component by text length so
    # distinct documents get distinct vectors without calling Bedrock.
    monkeypatch.setattr(
        knn_hydration, "embed_text",
        lambda text: [float(len(text) % 7) + 1.0] + [0.0] * (
            knn_hydration.EMBED_DIM - 1
        ),
    )


def test_cold_start_search_hydrates_instead_of_raising(monkeypatch):
    """The shipped bug: searching a never-built index raised IndexNotFoundError.

    Drives the whole real path — router closure → ``_hydrate_topic_router`` →
    S3 corpus listing → embedding → KNN upsert → search.
    """
    _stub_aws(monkeypatch, objects={
        FEDERATED_KEY: DOC,
        FEDERATED_KEY + ".metadata.json": "{}",
        PREFIX + "s3tablescatalog/bucket/normalized/party.md":
            "# p\n\n## Overview\nParties: people and organizations.\n",
    })

    router = main._build_topic_router(
        "hybrid-curated_layer", layer_id="hybrid-curated_layer", version="v1",
    )
    # Drive the search seam with a fixed vector so the assertion is about
    # hydration having happened, not about embedding fidelity.
    hits = router.knn_search(vector=[1.0] + [0.0] * 1023, k=5)

    assert {h["id"] for h in hits} == {"normalized.address", "normalized.party"}


def test_hydration_is_idempotent_across_queries(monkeypatch):
    """Only the first query per container pays the embedding cost."""
    calls = []
    _stub_aws(monkeypatch, objects={FEDERATED_KEY: DOC})
    inner = knn_hydration.embed_text
    monkeypatch.setattr(
        knn_hydration, "embed_text",
        lambda text: (calls.append(text), inner(text))[1],
    )

    router = main._build_topic_router(
        "hybrid-curated_layer", layer_id="hybrid-curated_layer", version="v1",
    )
    router.knn_search(vector=[1.0] + [0.0] * 1023, k=5)
    router.knn_search(vector=[1.0] + [0.0] * 1023, k=5)

    assert len(calls) == 1, "second query must not re-embed the corpus"


def test_search_returns_empty_when_index_absent_after_hydration(monkeypatch):
    """A still-missing index degrades to empty recall, not an aborted invocation."""
    monkeypatch.setattr(main, "_hydrate_topic_router", lambda **kw: 0)

    router = main._build_topic_router("ns-with-no-index")

    assert router.knn_search(vector=[1.0] + [0.0] * 1023, k=5) == []


def test_hydration_requires_artifacts_bucket(monkeypatch):
    """Missing ARTIFACTS_BUCKET must fail loud rather than skip hydration."""
    monkeypatch.delenv("ARTIFACTS_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="ARTIFACTS_BUCKET"):
        main._hydrate_topic_router(namespace="ns", layer_id="ns", version="v1")


# --------------------------------------------------------------------------- #
# CHUNKS LOOKUP — the slice builder's schema source. Without it SliceBuilder     #
# leaves `columns`/`joins` EMPTY (see its own docstring), so the Phase-3 judge    #
# sees table names with no columns, reports the columns it needs as "missing",    #
# expand() finds nothing new to add, and the graph degrades                       #
# `phase3_max_rounds` claiming a table is unavailable that it is in fact          #
# holding. Measured: 4 of 18 eval runs died this way, one of them naming          #
# `normalized.life_participant` — a table another run queried successfully.       #
# --------------------------------------------------------------------------- #
def _chunks(keys, *, bodies=None):
    """Build a chunks lookup over in-memory keys/bodies.

    Parameters:
        keys: The object keys the fake listing returns.
        bodies: Optional ``{key: markdown}`` map; defaults every key to ``DOC``.
    Returns:
        Callable: the ``chunks_lookup(table_ids, namespace)`` callable under test.
    """
    bodies = bodies or {}
    return kb_table_index.build_chunks_lookup(
        layer_id="hybrid-curated_layer", version="v1",
        list_keys=lambda prefix: keys,
        get_text=lambda key: bodies.get(key, DOC),
    )


def test_chunks_lookup_returns_markdown_per_requested_table():
    """The lookup returns the raw KB markdown keyed by candidate id."""
    lookup = _chunks([FEDERATED_KEY, PREFIX + "cat/normalized/party.md"])

    got = lookup(table_ids=["normalized.address"], namespace="ns")

    assert set(got) == {"normalized.address"}
    # The BODY must be the full document — the slice builder parses the Columns
    # table out of it, which recall_text deliberately strips.
    assert "party_id" in got["normalized.address"]


def test_chunks_lookup_omits_tables_with_no_document():
    """A candidate with no KB document must be ABSENT, not present-and-empty.

    ``SliceBuilder._slice_dict`` narrows ``tables`` to the ids that produced a
    chunk, so a phantom id must not come back at all.
    """
    lookup = _chunks([FEDERATED_KEY])

    got = lookup(table_ids=["normalized.address", "normalized.nope"], namespace="ns")

    assert set(got) == {"normalized.address"}


def test_chunks_lookup_lists_once_across_calls():
    """The key map is listed once per lookup — not re-listed per candidate."""
    calls = []

    def _list(prefix):
        calls.append(prefix)
        return [FEDERATED_KEY, PREFIX + "cat/normalized/party.md"]

    lookup = kb_table_index.build_chunks_lookup(
        layer_id="hybrid-curated_layer", version="v1",
        list_keys=_list, get_text=lambda key: DOC,
    )
    lookup(table_ids=["normalized.address"], namespace="ns")
    lookup(table_ids=["normalized.party"], namespace="ns")

    assert len(calls) == 1, "the KB key listing must be cached across calls"


def test_hybrid_deps_wire_a_chunks_lookup(monkeypatch):
    """The shipped gap: ``SliceBuilder`` was built with NO chunk source.

    Its ``columns``/``joins`` therefore stayed empty and every question whose
    judge wanted a specific column degraded ``phase3_max_rounds``.
    """
    monkeypatch.setenv("ARTIFACTS_BUCKET", "bucket")
    monkeypatch.setattr(main, "_build_topic_router",
                        lambda *a, **kw: types.SimpleNamespace())
    monkeypatch.setattr(main, "_build_slice_judge", lambda: None)
    monkeypatch.setattr(main, "_build_sql_generator", lambda: None)
    monkeypatch.setattr(
        main, "get_boto_session",
        lambda: types.SimpleNamespace(
            client=lambda *a, **kw: _FakeS3({FEDERATED_KEY: DOC}),
        ),
    )

    deps = main._build_hybrid_deps(
        kb_id="kb", namespace="curated_layer",
        config={"id": "hybrid-curated_layer", "version": "v1"},
    )

    assert deps.builder.chunks_lookup is not None
    got = deps.builder.chunks_lookup(
        table_ids=["normalized.address"], namespace="curated_layer",
    )
    assert "party_id" in got["normalized.address"]


def test_deps_scope_corpus_by_layer_id_not_namespace(monkeypatch):
    """The KB corpus is keyed by layer id + version, which may differ from ns."""
    seen = {}

    def _capture(namespace, *, layer_id="", version="v1"):
        seen.update(namespace=namespace, layer_id=layer_id, version=version)
        return types.SimpleNamespace()

    monkeypatch.setattr(main, "_build_topic_router", _capture)
    monkeypatch.setattr(main, "_build_slice_judge", lambda: None)
    monkeypatch.setattr(main, "_build_sql_generator", lambda: None)
    # The deps builder now also wires the Phase-3 chunk source, which reads S3.
    monkeypatch.setenv("ARTIFACTS_BUCKET", "bucket")
    monkeypatch.setattr(
        main, "get_boto_session",
        lambda: types.SimpleNamespace(client=lambda *a, **kw: _FakeS3({})),
    )

    main._build_hybrid_deps(
        kb_id="kb", namespace="curated_layer",
        config={"id": "hybrid-curated_layer", "version": "v3"},
    )

    assert seen == {
        "namespace": "curated_layer",
        "layer_id": "hybrid-curated_layer",
        "version": "v3",
    }
