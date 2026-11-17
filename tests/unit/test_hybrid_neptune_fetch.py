from agents.semantic_layer_query_agent.neptune_fetch import StructuralGraphFetcher
def test_fetches_and_caches():
    calls = {"n": 0}
    def fake_construct(*, namespace, candidates):
        calls["n"] += 1
        return "@prefix vkg: <https://x#> .\n<http://x/Address> vkg:mapsToTable \"address\" ."
    f = StructuralGraphFetcher(construct_ttl=fake_construct)
    g1 = f.get(namespace="ns", candidates=["http://x/Address"])
    g2 = f.get(namespace="ns", candidates=["http://x/Address"])
    assert calls["n"] == 1 and len(g1) > 0 and g1 is g2   # cached per (namespace, candidates)
def test_fetch_failure_degrades_loud():
    def boom(**k): raise RuntimeError("gateway down")
    f = StructuralGraphFetcher(construct_ttl=boom)
    import pytest
    with pytest.raises(RuntimeError):
        f.get(namespace="ns", candidates=["x"])   # fail loud — never silent single-engine
