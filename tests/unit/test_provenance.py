"""Unit tests for agents.shared.provenance.build_provenance.

Covers the uniform provenance object shape, the degraded passthrough, the
fail-loud guard on an unknown tier, and that source lists are copied (not
aliased). The query-agent response dicts and both chat ``totals`` builders thread
this object; those integrations are covered by the agents' own tests — here we
pin the contract the UI badge + eval harness depend on.
"""
import pathlib
import re
from types import SimpleNamespace

import pytest

from agents.shared import provenance
from agents.shared.provenance import build_provenance, VALID_TIERS


def test_governed_metric_shape():
    """A Tier 1 metric provenance carries tier, grounded, sources, degraded."""
    prov = build_provenance(tier="governed_metric", sources=["metric:revenue_ttm"])
    assert prov == {
        "tier": "governed_metric",
        "grounded": True,
        "sources": ["metric:revenue_ttm"],
        "degraded": None,
    }


def test_semantic_sql_with_tables_and_degraded():
    """Tier 2 provenance carries table sources and a degraded terminal reason."""
    prov = build_provenance(
        tier="semantic_sql",
        sources=["table:coverage", "table:holding"],
        degraded="phase3_max_rounds",
    )
    assert prov["tier"] == "semantic_sql"
    assert prov["grounded"] is True
    assert prov["sources"] == ["table:coverage", "table:holding"]
    assert prov["degraded"] == "phase3_max_rounds"


def test_vkg_and_advisory_are_valid_tiers():
    """VKG and advisory are recognized tiers and build cleanly."""
    assert build_provenance(tier="vkg", sources=["class:Party"])["tier"] == "vkg"
    assert build_provenance(tier="advisory", sources=["kb"])["tier"] == "advisory"


def test_agentic_is_a_valid_tier():
    """Tier 3 has its own tier so a rescued turn is not counted as Tier 2.

    Regression guard: both query agents used to stamp ``vkg``/``semantic_sql``
    even when the Tier 3 supervisor loop produced the answer, and Tier 3 success
    also clears ``degraded`` — so a rescued turn was byte-identical to a clean
    Tier 2 run and the monitoring tab reported the agentic layer as always-zero.
    """
    prov = build_provenance(tier="agentic", sources=["table:claim"])
    assert prov["tier"] == "agentic"
    assert prov["grounded"] is True
    # A recovered Tier 3 answer executed and was judge-accepted, so it is NOT
    # degraded; the tier alone records that an escalation happened.
    assert prov["degraded"] is None


def test_governed_rule_is_a_valid_tier():
    """A rule answer needs its own tier before it can be produced at all.

    ``build_provenance`` raises on an unknown tier, so the rule executor's happy
    path throws until this is registered — the failure would surface as a broken
    answer, not as a mislabelled badge.
    """
    prov = build_provenance(tier="governed_rule", sources=["rule:RULE-004"])
    assert prov["tier"] == "governed_rule"
    # Grounded: a rule answer rests on executed SQL AND an approved rule, which
    # is strictly more evidence than the semantic tier carries.
    assert prov["grounded"] is True


def test_an_unregistered_tier_still_raises():
    """The fail-loud contract must survive adding a tier."""
    with pytest.raises(ValueError):
        build_provenance(tier="governed_hunch", sources=[])


def test_all_valid_tiers_build():
    """Every declared tier in VALID_TIERS must build without error."""
    for tier in VALID_TIERS:
        prov = build_provenance(tier=tier, sources=[])
        assert prov["tier"] == tier
        assert prov["grounded"] is True
        assert prov["sources"] == []


def test_the_hybrid_agents_copied_tier_list_agrees():
    """The copy-and-own copy must not drift from the shared list.

    ``agents/semantic_layer_query_agent/provenance.py`` is a deliberate copy (the
    Hybrid isolation rule forbids importing ``agents.shared``), and its
    ``build_provenance`` RAISES on an unrecognised tier. So a tier added here but
    not there is a 500 on the hybrid path the first time that tier is emitted —
    which is exactly the kind of bug that only shows up in production. Read the
    tuple out of the source text rather than importing it, so the test does not
    itself breach the isolation boundary it is protecting.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "agents" / "semantic_layer_query_agent" / "provenance.py"
    ).read_text(encoding="utf-8")
    match = re.search(r"VALID_TIERS = \(([^)]*)\)", source)
    assert match, "VALID_TIERS tuple not found in the hybrid agent's provenance.py"
    copied = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    assert copied == set(VALID_TIERS)


def test_unknown_tier_raises():
    """An unrecognized tier fails loud rather than emitting an unrenderable badge."""
    with pytest.raises(ValueError, match="unknown provenance tier"):
        build_provenance(tier="freeform", sources=[])


def test_sources_are_copied_not_aliased():
    """Mutating the caller's list must not mutate the provenance object."""
    src = ["table:a"]
    prov = build_provenance(tier="semantic_sql", sources=src)
    src.append("table:b")
    assert prov["sources"] == ["table:a"]


def test_provenance_omits_new_blocks_when_absent():
    """Legacy shape is byte-identical — MonitoringTab and eval notebooks keep working."""
    p = build_provenance(tier="semantic_sql", sources=["table:t"])
    assert set(p) == {"tier", "grounded", "sources", "degraded"}


def test_provenance_carries_related_metrics():
    """A near-miss reaches the UI with its score and the reason it wasn't used."""
    ref = {"metric_id": "net_revenue", "score": 0.83, "reason": "below_threshold"}
    p = build_provenance(
        tier="semantic_sql", sources=["table:t"], related_metrics=[ref],
    )
    assert p["related_metrics"] == [ref]
    assert "metric" not in p


def test_provenance_carries_used_metric():
    """Tier 1 states which metric answered and how it was scoped."""
    used = {"metric_id": "net_revenue", "binding": {"bound": {"region": "EMEA"}}}
    p = build_provenance(
        tier="governed_metric", sources=["metric:net_revenue"], metric=used,
    )
    assert p["metric"] == used
    assert "related_metrics" not in p


def test_empty_related_metrics_list_is_omitted_not_empty():
    """An empty list must not render an empty card — omit the key entirely."""
    p = build_provenance(tier="semantic_sql", sources=[], related_metrics=[])
    assert "related_metrics" not in p


def test_metric_ref_shape_and_reason():
    """metric_ref is the single place the card contract is defined."""
    m = SimpleNamespace(
        metric_id="net_revenue", name="Net Revenue (TTM)",
        description="gross - returns", lifecycle="PUBLISHED", version=3,
        owner="finance-analytics",
    )
    ref = provenance.metric_ref(metric=m, score=0.83, reason="below_threshold")
    assert ref == {
        "metric_id": "net_revenue", "name": "Net Revenue (TTM)",
        "description": "gross - returns", "lifecycle": "PUBLISHED", "version": 3,
        "owner": "finance-analytics", "score": 0.83, "reason": "below_threshold",
    }


def test_metric_ref_omits_owner_when_unset():
    """No owner authored ⇒ the key is absent, never the string 'unknown'."""
    m = SimpleNamespace(
        metric_id="m", name="M", description="d", lifecycle="PUBLISHED", version=1,
    )
    assert "owner" not in provenance.metric_ref(metric=m, score=0.9, reason="used")


def test_metric_ref_unwraps_lifecycle_enum():
    """A MetricLifecycle enum must reach the SSE frame as a plain string.

    An enum here is not JSON-serializable, so the whole turn would fail to
    stream — a provenance detail breaking the answer.
    """
    from agents.shared.metric_models import MetricLifecycle
    m = SimpleNamespace(
        metric_id="m", name="M", description="d",
        lifecycle=MetricLifecycle.PUBLISHED, version=1,
    )
    ref = provenance.metric_ref(metric=m, score=0.9, reason="used")
    assert ref["lifecycle"] == "PUBLISHED"
    assert isinstance(ref["lifecycle"], str)
    import json
    json.dumps(ref)  # must not raise
