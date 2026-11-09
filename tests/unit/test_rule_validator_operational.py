"""Level 3 — operational validation: do bindings resolve to real columns?"""
from agents.shared.rule_validator import validate_operational


def _select(rows):
    """Build a fake SPARQL SELECT callable returning `rows` for any query."""

    def _call(*, query: str, graph_uri: str):
        return rows

    return _call


def test_fully_mapped_bindings_pass_with_no_missing_inputs() -> None:
    report, missing = validate_operational(
        bindings=["https://ex.org/vkg#days"],
        select=_select([{"binding": "https://ex.org/vkg#days",
                        "column": "holdings.days_since_last_payment"}]),
        ontology_graph_uri="https://g/ont-1",
    )
    assert report.passed is True
    assert missing == []


def test_an_unmapped_binding_is_a_missing_input_NOT_a_validation_failure() -> None:
    """THE distinction this level exists to draw.

    An unmapped binding does not make the rule invalid — it makes it
    non-evaluable. The rule may still publish and answer via advisory. If this
    level reported passed=False here, the publish gate would reject the rule and
    the entire advise-rather-than-evaluate path would become unreachable.
    """
    report, missing = validate_operational(
        bindings=["https://ex.org/vkg#coolingCapacity"],
        select=_select([]),
        ontology_graph_uri="https://g/ont-1",
    )
    assert report.passed is True
    assert missing == ["https://ex.org/vkg#coolingCapacity"]
    # Still surfaced to the reviewer, as informational rather than blocking.
    assert any("coolingCapacity" in v for v in report.violations)


def test_partially_mapped_rule_returns_only_the_unmapped_bindings() -> None:
    rows = [{"binding": "https://ex.org/vkg#days", "column": "h.days"}]
    report, missing = validate_operational(
        bindings=["https://ex.org/vkg#days", "https://ex.org/vkg#status"],
        select=_select(rows),
        ontology_graph_uri="https://g/ont-1",
    )
    assert missing == ["https://ex.org/vkg#status"]
    assert any("status" in v for v in report.violations)


def test_missing_inputs_are_returned_in_a_stable_order() -> None:
    """Ordering must be deterministic so the UI and stored rule agree."""
    _report, missing = validate_operational(
        bindings=["https://ex.org/vkg#b", "https://ex.org/vkg#a"],
        select=_select([]),
        ontology_graph_uri="https://g/ont-1",
    )
    assert missing == ["https://ex.org/vkg#b", "https://ex.org/vkg#a"]


def test_a_blank_column_value_counts_as_unmapped() -> None:
    """An empty mapsToColumn literal is drift, not a mapping."""
    _report, missing = validate_operational(
        bindings=["https://ex.org/vkg#days"],
        select=_select([{"binding": "https://ex.org/vkg#days", "column": "  "}]),
        ontology_graph_uri="https://g/ont-1",
    )
    assert missing == ["https://ex.org/vkg#days"]


def test_a_select_error_fails_closed_and_marks_everything_missing() -> None:
    """A FAILED PROBE is different from a KNOWN-unmapped binding.

    Unmapped-but-probed is data (passed=True, routes to advisory). A probe that
    never completed proves nothing, so it must block publish outright — we must
    never publish a rule whose data mapping we could not verify.
    """
    def boom(*, query: str, graph_uri: str):
        raise RuntimeError("neptune down")

    report, missing = validate_operational(
        bindings=["https://ex.org/vkg#days"],
        select=boom,
        ontology_graph_uri="https://g/ont-1",
    )
    assert report.passed is False
    assert missing == ["https://ex.org/vkg#days"]
