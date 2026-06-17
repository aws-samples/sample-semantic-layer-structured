"""Unit tests for AG-UI tier/phase events (progressive disclosure)."""
from agents.shared.agui_emitter import AGUIEmitter


def test_emit_tier1_hit_event():
    e = AGUIEmitter(turn_id="t-1")
    e.emit_tier(tier=1, phase=None, action="metric_hit",
                payload={"metric_id": "monthly_revenue"})
    events = e.drain()
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "tier_event"
    assert ev["tier"] == 1
    assert ev["action"] == "metric_hit"
    assert ev["metric_id"] == "monthly_revenue"
    assert ev["turnId"] == "t-1"


def test_emit_tier2_phase_event():
    e = AGUIEmitter(turn_id="t-2")
    e.emit_tier(tier=2, phase=2, action="slice_round",
                payload={"round": 2, "slice_size_chars": 9012})
    ev = e.drain()[0]
    assert ev["tier"] == 2
    assert ev["phase"] == 2
    assert ev["action"] == "slice_round"
    assert ev["round"] == 2
