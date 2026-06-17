"""Unit tests for agents.shared.provenance.build_provenance.

Covers the uniform provenance object shape, the degraded passthrough, the
fail-loud guard on an unknown tier, and that source lists are copied (not
aliased). The query-agent response dicts and both chat ``totals`` builders thread
this object; those integrations are covered by the agents' own tests — here we
pin the contract the UI badge + eval harness depend on.
"""
import pytest

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


def test_all_valid_tiers_build():
    """Every declared tier in VALID_TIERS must build without error."""
    for tier in VALID_TIERS:
        prov = build_provenance(tier=tier, sources=[])
        assert prov["tier"] == tier
        assert prov["grounded"] is True
        assert prov["sources"] == []


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
