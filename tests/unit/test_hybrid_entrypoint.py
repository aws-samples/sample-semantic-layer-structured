"""Hybrid query-agent entrypoint: artifact-guard + Tier-2 delegation.

``main._resolve`` MUST assert BOTH hybrid artifacts (Bedrock KB + thin VKG)
exist before answering. If either is missing it returns a
``hybrid_artifact_missing`` degraded result — never a silent single-engine
fallback. When both exist it delegates to ``_run_tier2`` (monkeypatched here).
"""


def test_missing_artifact_degrades():
    from agents.semantic_layer_query_agent import main
    res = main._resolve(payload={"question": "q", "ontologyId": "o"},
                        artifacts={"kb": True, "vkg": False})
    assert res["degraded"] == "hybrid_artifact_missing"


def test_both_artifacts_present_proceeds(monkeypatch):
    from agents.semantic_layer_query_agent import main
    # when both artifacts exist, _resolve must NOT short-circuit to the missing-artifact path
    monkeypatch.setattr(main, "_run_tier2", lambda payload: {"answer": "ok", "degraded": None})
    res = main._resolve(payload={"question": "q", "ontologyId": "o"},
                        artifacts={"kb": True, "vkg": True})
    assert res.get("degraded") != "hybrid_artifact_missing"


def test_run_tier2_passes_the_layers_catalog_to_the_workflow(monkeypatch):
    """The catalog must be read from the layer config, not left to default.

    An S3 Tables layer's schema only resolves when its federated catalog is named
    in the Athena execution context. This pins the config -> workflow hop, which
    was missing and made every Phase-5 execution fail SCHEMA_NOT_FOUND despite a
    correctly grounded query.
    """
    from agents.semantic_layer_query_agent import main

    catalog = "s3tablescatalog/semantic-layer-dev-analytics-tables"
    config = {
        "id": "hybrid-l1",
        "namespace": "hybrid-l1",
        "version": "v1",
        "dataSources": [{"catalogId": catalog, "databaseName": "normalized"}],
    }
    seen = {}

    monkeypatch.setattr(main, "get_latest_metadata_item", lambda _id: config)
    monkeypatch.setattr(main, "_kb_id_for", lambda _cfg: "kb-1")
    monkeypatch.setattr(main, "_build_hybrid_deps", lambda **kwargs: object())
    monkeypatch.setattr(main, "_normalize_tier2", lambda **kwargs: {"answer": "ok"})
    monkeypatch.setattr(
        main, "run_hybrid_workflow",
        lambda **kwargs: seen.update(kwargs) or object(),
    )

    main._run_tier2({"question": "q", "id": "hybrid-l1"})
    assert seen["catalog_id"] == catalog
