"""Emitting inferred relationships into the Hybrid thin VKG, end to end.

Three separate omissions each produced the same visible symptom — a Hybrid layer
whose graph viewer said "No relationships found":

1. ``_make_build_vkg`` never passed ``joins`` to the emitter at all, so no
   ``owl:ObjectProperty`` could exist in the graph;
2. an emitted object property without ``rdfs:range`` is neither queryable (the
   OBDA mapping cannot unify it with the target's subject template) nor
   displayable (the viewer reads range as the "To Entity" column);
3. a primary key must be MEASURED, because declaring one also licenses Ontop to
   drop de-duplication — so a name-based key claim corrupts results.

These tests pin all three plus the S3 artifact the Metadata tab reads.
"""
import pytest

from agents.semantic_layer_creation_agent import athena_probe, main
from agents.semantic_layer_creation_agent.vkg_builder import build_thin_vkg_nquads

OBJECT_PROPERTY = "<http://www.w3.org/2002/07/owl#ObjectProperty>"
RDFS_RANGE = "<http://www.w3.org/2000/01/rdf-schema#range>"


# --- vkg_builder: the join hook ------------------------------------------------
def test_join_hook_emits_object_property_with_range_and_mapping():
    nquads = build_thin_vkg_nquads(
        table="cards",
        columns=[{"name": "uuid", "type": "string"}, {"name": "setCode"}],
        class_iri="http://x/cards",
        joins=[
            {
                "property": "http://x/cards/hasSets",
                "target_column": "cards.setCode",
                "range": "http://x/sets",
                "label": "hasSets",
            }
        ],
    )
    assert f"<http://x/cards/hasSets> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> {OBJECT_PROPERTY}" in nquads
    assert f"<http://x/cards/hasSets> {RDFS_RANGE} <http://x/sets>" in nquads
    assert '"hasSets"' in nquads                      # viewer's relationship name
    assert "cards.setCode" in nquads                  # Ontop mapping to the FK


def test_join_hook_without_range_fails_loud():
    """A range-less edge reformulates to zero rows and shows no target entity."""
    with pytest.raises(ValueError, match="range"):
        build_thin_vkg_nquads(
            table="cards",
            columns=[{"name": "setCode"}],
            class_iri="http://x/cards",
            joins=[{"property": "http://x/cards/hasSets",
                    "target_column": "cards.setCode"}],
        )


# --- athena_probe: measured uniqueness ----------------------------------------
def test_probe_accepts_only_columns_that_are_unique_and_non_null():
    captured = {}

    def run_query(sql):
        """Record the SQL and answer with one aggregate row."""
        captured["sql"] = sql
        # code: distinct == non_null == total -> a key.
        # name: distinct < total -> not unique.
        # slug: distinct == total but one NULL -> cannot template into an IRI.
        return [{"n": "10", "d_0": "10", "nn_0": "10",
                 "d_1": "7", "nn_1": "10",
                 "d_2": "10", "nn_2": "9"}]

    unique = athena_probe.probe_unique_columns(
        table="sets", columns=["code", "name", "slug"],
        database="db", catalog="AwsDataCatalog", run_query=run_query,
    )
    assert unique == ["code"]
    # One query per table, not per column.
    assert captured["sql"].count("FROM") == 1


def test_probe_of_an_empty_table_declares_no_key():
    unique = athena_probe.probe_unique_columns(
        table="sets", columns=["code"], database="db", catalog="c",
        run_query=lambda sql: [{"n": "0", "d_0": "0", "nn_0": "0"}],
    )
    assert unique == []


def test_probe_with_no_rows_raises_rather_than_reporting_not_unique():
    with pytest.raises(RuntimeError, match="no rows"):
        athena_probe.probe_unique_columns(
            table="sets", columns=["code"], database="db", catalog="c",
            run_query=lambda sql: [],
        )


# --- main._make_build_vkg: the wiring ----------------------------------------
COLUMNS = {
    "cards": [{"name": "uuid", "type": "string"},
              {"name": "setCode", "type": "string"}],
    "sets": [{"name": "code", "type": "string"},
             {"name": "name", "type": "string"}],
}

TABLES = [
    {"database": "db", "table": "cards", "catalogId": "AwsDataCatalog"},
    {"database": "db", "table": "sets", "catalogId": "AwsDataCatalog"},
]


def _wire(monkeypatch, *, unique_columns):
    """Stub every AWS seam ``_make_build_vkg`` touches and capture its output.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        unique_columns: What the uniqueness probe should report, as
            ``{table: [column, ...]}``.

    Returns:
        dict: A capture dict populated with ``nquads``, ``s3`` and ``stamp``.
    """
    captured = {}
    monkeypatch.setattr(
        main, "_glue_columns",
        lambda *, database, table, catalog_id: COLUMNS[table],
    )
    monkeypatch.setattr(
        athena_probe, "probe_unique_columns",
        lambda **kwargs: unique_columns.get(kwargs["table"], []),
    )
    monkeypatch.setattr(
        main, "_load_vkg_into_neptune",
        lambda *, namespace, nquads: captured.__setitem__("nquads", nquads),
    )
    monkeypatch.setattr(
        main, "_save_vkg_to_s3",
        lambda *, id, nquads: captured.setdefault("s3", f"s3://b/ontologies/{id}/ontology.nq"),
    )
    monkeypatch.setattr(
        main, "_stamp_vkg_built",
        lambda **kwargs: captured.__setitem__("stamp", kwargs),
    )
    # Descriptions are harvested from the KB documents in S3; these tests are about
    # the structural wiring, so stub the harvest away rather than stubbing S3.
    # Their own coverage lives in tests/unit/test_hybrid_property_descriptions.py.
    monkeypatch.setattr(main, "_harvest_kb_descriptions", lambda **kwargs: {})
    return captured


def test_build_vkg_emits_the_inferred_relationship(monkeypatch):
    """The whole point: a Hybrid graph must contain object properties."""
    captured = _wire(monkeypatch, unique_columns={"sets": ["code"]})
    main._make_build_vkg(id="lay", version="v1", config={}, tables=TABLES)()

    nquads = captured["nquads"]
    assert OBJECT_PROPERTY in nquads
    assert "hasSets" in nquads
    assert "cards.setCode" in nquads


def test_build_vkg_declares_only_the_measured_key(monkeypatch):
    """`sets.code` was measured unique; `cards.setCode` was not — so no key."""
    captured = _wire(monkeypatch, unique_columns={"sets": ["code"]})
    main._make_build_vkg(id="lay", version="v1", config={}, tables=TABLES)()

    nquads = captured["nquads"]
    assert "hasPrimaryKey" in nquads
    assert "sets.code" in nquads
    # A key on the many side would license Ontop to drop de-duplication.
    assert "cards.uuid" not in nquads.split("hasPrimaryKey", 1)[1].split("\n")[0]


def test_build_vkg_declares_no_key_when_nothing_measures_unique(monkeypatch):
    """An unverified key is worse than none, so the graph must simply omit it."""
    captured = _wire(monkeypatch, unique_columns={})
    main._make_build_vkg(id="lay", version="v1", config={}, tables=TABLES)()
    assert "hasPrimaryKey" not in captured["nquads"]


def test_build_vkg_reports_progress_and_stamps_the_metadata_path(monkeypatch):
    captured = _wire(monkeypatch, unique_columns={"sets": ["code"]})
    seen = []
    main._make_build_vkg(
        id="lay", version="v1", config={}, tables=TABLES,
        report_progress=lambda **kwargs: seen.append(kwargs),
    )()

    assert [event["current"] for event in seen] == ["cards", "sets"]
    assert seen[-1] == {"phase": "vkg", "done": 2, "total": 2, "current": "sets"}
    # metadataPath is what the Metadata tab resolves; without it that tab errors.
    assert captured["stamp"]["metadata_path"] == "s3://b/ontologies/lay/ontology.nq"


def test_save_vkg_to_s3_writes_where_the_metadata_tab_reads(monkeypatch):
    """``get_metadata_content`` strips the bucket off metadataPath and GETs the key."""
    captured = {}

    class _S3:
        def put_object(self, **kwargs):
            """Record the put instead of calling S3."""
            captured.update(kwargs)

    monkeypatch.setenv("ARTIFACTS_BUCKET", "bkt")
    monkeypatch.setattr(
        main, "get_boto_session", lambda: type("S", (), {"client": lambda self, n: _S3()})()
    )
    uri = main._save_vkg_to_s3(id="lay", nquads="<a> <b> <c> <g> .\n")
    assert uri == "s3://bkt/ontologies/lay/ontology.nq"
    assert captured["Key"] == "ontologies/lay/ontology.nq"
    assert captured["ContentType"] == "application/n-quads"


def test_save_vkg_to_s3_without_a_bucket_fails_loud(monkeypatch):
    """A completed layer whose artifact is missing errors on every metadata read."""
    monkeypatch.delenv("ARTIFACTS_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="ARTIFACTS_BUCKET"):
        main._save_vkg_to_s3(id="lay", nquads="x")


def test_a_failed_key_probe_degrades_to_no_key_rather_than_failing_the_build(
    monkeypatch,
):
    """Key unknown is a degraded build; a wrong key is a corrupt one."""
    captured = _wire(monkeypatch, unique_columns={"sets": ["code"]})

    def boom(**kwargs):
        raise RuntimeError("athena down")

    monkeypatch.setattr(athena_probe, "probe_unique_columns", boom)
    main._make_build_vkg(id="lay", version="v1", config={}, tables=TABLES)()
    assert "hasPrimaryKey" not in captured["nquads"]
    assert OBJECT_PROPERTY in captured["nquads"]   # edges still emitted
