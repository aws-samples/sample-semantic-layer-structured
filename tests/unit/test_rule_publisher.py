"""The publish gate: every guard, with all collaborators injected."""
import pytest

from agents.shared.rule_models import (
    Condition, Conclusion, Rule, RuleScope, Threshold,
)
from agents.shared.rule_publisher import PublishBlocked, publish_rule


def _rule(**over) -> Rule:
    base = dict(
        rule_id="RULE-004", namespace="ont-1", name="n", description="d",
        rule_type="diagnostic", level="contextualised", lifecycle="APPROVED",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days", operator=">",
                              threshold_id="THR-011")],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id="THR-011", value=90.0, unit="days",
                              source_span="after 90 days", approved_by="alice@x")],
    )
    base.update(over)
    return Rule(**base)


def _ok_deps(**over):
    deps = dict(
        ask=lambda **_kw: True,
        select=lambda **_kw: [{"binding": "https://ex.org/vkg#days",
                               "column": "h.days"}],
        translate=lambda **_kw: {"sql": "SELECT 1", "database": "db",
                                 "catalog": "cat"},
        execute_sql=lambda **_kw: {"rows": []},
        ontology_graph_uri="https://g/ont-1",
        ontology_json={"classes": [], "mappings": []},
        # Deliberately DIFFERENT from the translation's "db"/"cat" above, so any
        # test that asserts on what reached Athena can tell the two apart.
        coordinate={"database": "layer_db", "catalog": "layer_cat"},
    )
    deps.update(over)
    return deps


def test_a_clean_rule_publishes_and_gains_compiled_sparql() -> None:
    published = publish_rule(rule=_rule(), **_ok_deps())
    assert published.lifecycle == "PUBLISHED"
    assert published.compiled_sparql and "FILTER" in published.compiled_sparql
    assert len(published.test_cases) == 5


def test_the_input_rule_is_not_mutated() -> None:
    """publish_rule returns a NEW rule — callers keep the original for rollback."""
    rule = _rule()
    publish_rule(rule=rule, **_ok_deps())
    assert rule.lifecycle == "APPROVED"
    assert rule.compiled_sparql is None


def test_a_failing_validation_level_blocks_publish() -> None:
    with pytest.raises(PublishBlocked, match="semantic"):
        publish_rule(rule=_rule(), **_ok_deps(ask=lambda **_kw: False))


def test_missing_inputs_publish_as_ADVISORY_ONLY_not_blocked() -> None:
    """The design's "advise when inputs are missing" half.

    Blocking here would delete the knowledge that a gap exists. Publishing
    advisory-only is what lets the query path NAME the gap to the user.
    """
    published = publish_rule(rule=_rule(), **_ok_deps(select=lambda **_kw: []))
    assert published.lifecycle == "PUBLISHED"
    assert published.missing_inputs == ["https://ex.org/vkg#days"]
    assert published.compiled_sparql == ""
    assert published.is_evaluable() is False
    assert len(published.test_cases) == 5


def test_an_advisory_only_rule_never_touches_ontop_or_athena() -> None:
    """No compiled SPARQL means there is nothing to smoke-execute. Calling
    Ontop anyway would fail the publish for a rule that is legitimately
    advisory-only."""
    def fail(**_kw):
        raise AssertionError("must not be called for an advisory-only rule")

    published = publish_rule(
        rule=_rule(),
        **_ok_deps(select=lambda **_kw: [], translate=fail, execute_sql=fail),
    )
    assert published.lifecycle == "PUBLISHED"


def test_an_unapproved_threshold_publishes_advisory_only() -> None:
    """An unapproved threshold is a missing INPUT, not a broken rule — the
    human simply has not approved the bound yet. Same advisory treatment."""
    rule = _rule(thresholds=[Threshold(threshold_id="THR-011", value=90.0,
                                       unit="days", source_span="after 90 days")])
    published = publish_rule(rule=rule, **_ok_deps())
    assert published.lifecycle == "PUBLISHED"
    assert published.compiled_sparql == ""
    assert published.is_evaluable() is False


def test_a_probe_failure_DOES_block_publish() -> None:
    """Unmapped-but-known is advisory; could-not-check must never publish."""
    def boom(**_kw):
        raise RuntimeError("neptune down")

    with pytest.raises(PublishBlocked, match="operational"):
        publish_rule(rule=_rule(), **_ok_deps(select=boom))


def test_a_translation_failure_blocks_publish_naming_ontop() -> None:
    def boom(**_kw):
        raise RuntimeError("Ontop cannot reformulate")

    with pytest.raises(PublishBlocked, match="Ontop"):
        publish_rule(rule=_rule(), **_ok_deps(translate=boom))


def test_an_athena_failure_blocks_publish() -> None:
    def boom(**_kw):
        raise RuntimeError("SYNTAX_ERROR")

    with pytest.raises(PublishBlocked, match="SYNTAX_ERROR"):
        publish_rule(rule=_rule(), **_ok_deps(execute_sql=boom))


def test_translate_receives_a_limited_query_so_the_smoke_run_is_cheap() -> None:
    seen = {}

    def translate(**kwargs):
        seen.update(kwargs)
        return {"sql": "SELECT 1", "database": "db", "catalog": "cat"}

    publish_rule(rule=_rule(), **_ok_deps(translate=translate))
    assert "LIMIT 1" in seen["sparql"]
    assert seen["ontology_id"] == "ont-1"


def test_an_empty_smoke_result_still_publishes() -> None:
    """Zero rows is a valid answer — the gate proves translatability, not data."""
    published = publish_rule(rule=_rule(),
                             **_ok_deps(execute_sql=lambda **_kw: {"rows": []}))
    assert published.lifecycle == "PUBLISHED"


def test_a_draft_rule_cannot_skip_the_approved_state() -> None:
    with pytest.raises(PublishBlocked, match="APPROVED"):
        publish_rule(rule=_rule(lifecycle="DRAFT"), **_ok_deps())


# --- the smoke query's coordinate ------------------------------------------
# Regression cases for the defect measured on the live hybrid layer: Ontop
# derives `database` from the ontology's `mapsToTable`, which a thin VKG stores
# BARE, so the translation named the alphabetically-first TABLE as the schema
# and Athena answered SCHEMA_NOT_FOUND. The layer config is the only
# authoritative source, so it is the one the gate must use.


def test_the_smoke_query_uses_the_layer_coordinate_not_the_translations() -> None:
    seen = {}

    def execute_sql(**kwargs):
        seen.update(kwargs)
        return {"rows": []}

    publish_rule(
        rule=_rule(),
        **_ok_deps(
            # Exactly what the live hybrid layer's translation returned.
            translate=lambda **_kw: {"sql": "SELECT 1", "database": "cards",
                                     "catalog": "AwsDataCatalog"},
            execute_sql=execute_sql,
            coordinate={
                "database": "bird_card_games",
                "catalog": "s3tablescatalog/semantic-layer-dev-analytics-tables",
            },
        ),
    )
    assert seen["database"] == "bird_card_games"
    assert seen["catalog"] == (
        "s3tablescatalog/semantic-layer-dev-analytics-tables"
    )


def test_a_blank_database_blocks_publish_before_any_translation() -> None:
    """Cheap check first: no point paying for an Ontop call we cannot execute."""
    def fail(**_kw):
        raise AssertionError("must not translate without a coordinate")

    with pytest.raises(PublishBlocked, match="database"):
        publish_rule(
            rule=_rule(),
            **_ok_deps(translate=fail,
                       coordinate={"database": "", "catalog": "cat"}),
        )


def test_a_blank_catalog_blocks_publish() -> None:
    """A federated catalog cannot be defaulted — omitting it is how the live
    hybrid query path fails SCHEMA_NOT_FOUND, so silence is not an option."""
    with pytest.raises(PublishBlocked, match="catalog"):
        publish_rule(rule=_rule(),
                     **_ok_deps(coordinate={"database": "db", "catalog": ""}))


def test_the_blocked_coordinate_reason_names_the_rule() -> None:
    """The message reaches the admin UI verbatim, so it must say which rule."""
    with pytest.raises(PublishBlocked, match="RULE-004"):
        publish_rule(rule=_rule(), **_ok_deps(coordinate={}))


def test_an_advisory_only_rule_publishes_without_a_coordinate() -> None:
    """The coordinate guard belongs to the EVALUABLE branch only. An
    advisory-only rule never executes, so a layer with no resolvable coordinate
    must not stop its mined knowledge from being published."""
    published = publish_rule(
        rule=_rule(),
        **_ok_deps(select=lambda **_kw: [], coordinate={}),
    )
    assert published.lifecycle == "PUBLISHED"
    assert published.compiled_sparql == ""
