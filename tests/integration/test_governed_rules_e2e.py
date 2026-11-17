"""Deploy-gated end-to-end test of the governed-rules loop.

REQUIRES LIVE AWS. Skipped unless ``RUN_GOVERNED_RULES_E2E=1`` and the layer
under test is named via ``GOVERNED_RULES_E2E_ONTOLOGY_ID``. Everything here
exists because it cannot be faked: Ontop reformulation of the compiled SPARQL,
the Neptune ``<id>/rules`` named-graph round-trip, and KNN hydration from a real
SPARQL SELECT. See the design doc §8 (deploy-gated tests).

Two layers of protection against this running by accident. ``pyproject.toml``
sets ``testpaths = ["tests/unit"]``, so a bare ``pytest`` never collects this
file; and the gate below skips with a printed reason when it is run by explicit
path without the env vars. It is flagged, never silently skipped — a developer
who runs it sees exactly which variables are missing.

Run::

    export RUN_GOVERNED_RULES_E2E=1
    export GOVERNED_RULES_E2E_ONTOLOGY_ID=<layer-id>
    export GOVERNED_RULES_E2E_SUBJECT_CLASS=<class IRI from that layer>
    export GOVERNED_RULES_E2E_BINDING=<mapped datatype-property IRI>
    export GOVERNED_RULES_E2E_OUTCOME=<outcome class IRI>
    export NEPTUNE_GATEWAY_URL=<the deployed REST Lambda's gateway url>
    export ONTOLOGY_METADATA_TABLE=<the layer-config table>
    python -m pytest tests/integration/test_governed_rules_e2e.py -v -s

``ONTOLOGY_METADATA_TABLE`` is where the layer's Athena catalog and database are
recorded. The publish gate's smoke query needs them from there and nowhere else:
Ontop derives a coordinate from the ontology, and a hybrid layer's thin VKG
stores bare table names, so that derivation names a table as the schema.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Iterator, List

import pytest

# `services.*` lives under the REST-API package root, not the repo root — the
# same insert tests/unit/test_neptune_service_rules.py makes. Done at import
# time (not in a fixture) because it touches nothing but sys.path.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "rest-api"),
)

_GATE = os.environ.get("RUN_GOVERNED_RULES_E2E", "") == "1"
_ONTOLOGY_ID = os.environ.get("GOVERNED_RULES_E2E_ONTOLOGY_ID", "")

pytestmark = pytest.mark.skipif(
    not (_GATE and _ONTOLOGY_ID),
    reason=(
        "deploy-gated: needs live AWS (Neptune gateway + Ontop + Athena + "
        "Bedrock embeddings). Set RUN_GOVERNED_RULES_E2E=1 and "
        "GOVERNED_RULES_E2E_ONTOLOGY_ID=<layer>, plus _SUBJECT_CLASS, "
        "_BINDING, _OUTCOME and NEPTUNE_GATEWAY_URL."
    ),
)

#: Rule id used throughout. Fixed, not random, so a crashed run leaves one
#: known-named rule an operator can delete by hand instead of litter.
_RULE_ID = "RULE-E2E-1"


def _athena_smoke_execute(*, sql: str, database: str, catalog: str) -> Dict[str, Any]:
    """Run the publish gate's smoke SQL, RAISING on any non-success.

    ``publish_rule`` only catches exceptions from its ``execute_sql`` callable —
    it never inspects the returned dict. But the agent-side helper
    ``_run_athena_sql`` reports a FAILED query by RETURNING a dict with
    ``state_change_reason`` set (it reserves raising for infra errors, so Phase 5
    can attempt SQL repair). Handing it to the gate unwrapped would let a rule
    whose smoke query failed publish anyway — the exact hole the smoke step
    exists to close. So the reason is re-raised here, matching the raise contract
    of the REST Lambda's own ``_execute_athena_sql`` (lambda/rest-api/main.py),
    which is what runs in production.

    :param sql: The SQL Ontop produced from the compiled rule.
    :param database: Athena schema, from the LAYER config (not the translation).
    :param catalog: Athena data catalog, from the LAYER config.
    :returns: The ``_run_athena_sql`` result dict on success.
    :raises RuntimeError: If Athena reported a terminal failure.
    """
    from agents.ontology_query_agent.main import _run_athena_sql

    result = _run_athena_sql(
        sql=sql, database_name=database, catalog_id=catalog,
    )
    reason = result.get("state_change_reason")
    if reason:
        raise RuntimeError(f"smoke query did not succeed: {reason}")
    return result


@pytest.fixture(scope="module")
def neptune() -> Any:
    """A live ``NeptuneService`` bound to the deployed gateway.

    :returns: The service instance.
    """
    from services.neptune_service import NeptuneService

    assert os.environ.get("NEPTUNE_GATEWAY_URL"), (
        "NEPTUNE_GATEWAY_URL must be set — read it from the deployed REST "
        "Lambda's configuration."
    )
    service = NeptuneService()
    # NeptuneService WARNS and leaves gateway_client None when the url is
    # missing; every later call would then fail with a generic
    # "Neptune Gateway not configured". Fail here instead, where the cause is
    # still obvious.
    assert service.gateway_client is not None, "gateway client did not initialise"
    return service


@pytest.fixture(scope="module")
def ontology_graph_uri(neptune: Any) -> str:
    """The newest ontology graph URI for the layer under test.

    :param neptune: The live service.
    :returns: The resolved graph URI.
    """
    return neptune.resolve_ontology_graph_uri(id=_ONTOLOGY_ID)


@pytest.fixture(scope="module")
def seeded_rule(neptune: Any) -> Iterator[Any]:
    """Hand-seed one fully-mapped rule, yield it, then delete it.

    Hand-seeded rather than doc-mined so this exercises the governance loop
    without depending on LLM extraction — the same reason the design sequences
    the authoring loop before ruleminer.

    The rule is created APPROVED. The human review transition is covered by the
    ``/rules`` router's own tests; what cannot be covered there is Ontop, and
    ``publish_rule`` refuses anything that is not APPROVED — seeding DRAFT would
    make the publish test fail with "only an APPROVED rule may be published"
    while looking like an Ontop finding.

    :param neptune: The live service.
    :yields: The seeded (unpersisted) rule.
    """
    from agents.shared.rule_models import (
        Conclusion, Condition, Provenance, Rule, RuleScope, Threshold,
    )

    subject_class = os.environ.get("GOVERNED_RULES_E2E_SUBJECT_CLASS", "")
    binding = os.environ.get("GOVERNED_RULES_E2E_BINDING", "")
    outcome = os.environ.get("GOVERNED_RULES_E2E_OUTCOME", "")
    assert subject_class and binding and outcome, (
        "Set GOVERNED_RULES_E2E_SUBJECT_CLASS / _BINDING / _OUTCOME to real "
        "IRIs from the layer under test — a rule bound to invented IRIs would "
        "fail semantic validation and tell us nothing about Ontop."
    )
    rule = Rule(
        rule_id=_RULE_ID,
        namespace=_ONTOLOGY_ID,
        name="e2e smoke rule",
        description="deploy-gated test rule",
        rule_type="diagnostic",
        level="contextualised",
        lifecycle="APPROVED",
        reviewed_by="e2e-test",
        scope=RuleScope(subject_class=subject_class),
        conditions=[Condition(binding=binding, operator=">", threshold_id="THR-1")],
        # approved_by set: an unapproved threshold publishes down the ADVISORY
        # branch with compiled_sparql empty, which would skip Ontop entirely.
        thresholds=[
            Threshold(threshold_id="THR-1", value=0.0, unit="",
                      source_span="test span", approved_by="e2e-test"),
        ],
        conclusion=Conclusion(outcome=outcome,
                              epistemic_status="candidateContributorTo"),
        provenance=Provenance(document_id="e2e", source_sentence="seeded by test",
                              model_id="none", confidence=1.0),
    )
    yield rule
    # Teardown runs even when a test failed, so a blocked publish does not leave
    # the rule behind. Same rule_id for the DRAFT and PUBLISHED copies, so one
    # delete clears both.
    neptune.delete_rule(namespace=_ONTOLOGY_ID, rule_id=_RULE_ID)


@pytest.fixture(scope="module")
def coordinate() -> Dict[str, str]:
    """Where the layer's tables live, resolved exactly as production resolves it.

    Deliberately goes through the REST Lambda's own
    ``_athena_coordinate_for_layer`` + ``OntologyService`` rather than taking two
    more env vars: the resolution IS part of what this test exists to prove. A
    hybrid layer's thin VKG cannot supply this coordinate, so reading it from the
    layer config is the only way the smoke query lands in the right schema.

    :returns: ``{"database", "catalog"}`` for the layer under test.
    """
    os.environ.setdefault("RULES_ENABLED", "true")
    import main
    from services.ontology_service import OntologyService

    assert os.environ.get("ONTOLOGY_METADATA_TABLE"), (
        "ONTOLOGY_METADATA_TABLE must be set — read it from the deployed REST "
        "Lambda's configuration; it is where the layer's catalog/database live."
    )
    return main._athena_coordinate_for_layer(
        config=OntologyService().get_metadata_config(id=_ONTOLOGY_ID),
        namespace=_ONTOLOGY_ID,
    )


@pytest.fixture(scope="module")
def published_rule(
    neptune: Any,
    seeded_rule: Any,
    ontology_graph_uri: str,
    coordinate: Dict[str, str],
) -> Any:
    """Run the real publish gate and persist the result.

    THE step that cannot be faked. A ``PublishBlocked`` here surfaces as a
    fixture error carrying Ontop's verbatim reason, and it is a legitimate
    finding about Ontop's current coverage of this rule shape — report it, do
    not weaken the gate to make it pass.

    Module-scoped so the publish runs once and the KNN test reads the same
    persisted rule; scoping it per-test would make the tests order-dependent.

    :param neptune: The live service.
    :param seeded_rule: The APPROVED rule to publish.
    :param ontology_graph_uri: The ontology graph (never the /rules graph).
    :param coordinate: Athena catalog/database the smoke query runs in.
    :returns: The PUBLISHED rule, already written to Neptune.
    """
    from agents.shared.rule_publisher import publish_rule

    published = publish_rule(
        rule=seeded_rule,
        ask=neptune.ask,
        select=neptune.select_in_graph,
        translate=neptune.translate_sparql_to_sql,
        execute_sql=_athena_smoke_execute,
        ontology_graph_uri=ontology_graph_uri,
        ontology_json=neptune.get_ontology(id=_ONTOLOGY_ID),
        coordinate=coordinate,
    )
    neptune.write_rule(rule=published)
    return published


def test_the_rules_graph_round_trips(neptune: Any, seeded_rule: Any) -> None:
    """Write to ``<prefix><id>/rules``, read it back, get the same rule.

    :param neptune: The live service.
    :param seeded_rule: The rule to persist.
    """
    neptune.write_rule(rule=seeded_rule)
    rules: List[Any] = neptune.read_rules(namespace=_ONTOLOGY_ID)
    # read_rules returns parsed Rule MODELS, not dicts — attribute access, and
    # arriving here at all proves the RDF re-parsed into a valid model.
    match = next((r for r in rules if r.rule_id == _RULE_ID), None)
    assert match is not None, f"{_RULE_ID} not in {[r.rule_id for r in rules]}"
    assert len(match.conditions) == len(seeded_rule.conditions)
    assert match.conditions[0].operator == ">"
    assert match.conditions[0].binding == seeded_rule.conditions[0].binding
    # Condition order must survive the round-trip. RDF is unordered, so this is
    # really a test of the explicit condition index written by rule_graph.
    assert [c.threshold_id for c in match.conditions] == [
        c.threshold_id for c in seeded_rule.conditions
    ]
    # The approver is what makes a threshold executable — losing it in the
    # round-trip would silently reroute every answer to the advisory branch.
    assert match.thresholds[0].approved_by == "e2e-test"


def test_the_ontology_graph_never_serves_rule_triples(
    neptune: Any, seeded_rule: Any, ontology_graph_uri: str,
) -> None:
    """The ``/rules`` exclusion, verified against real graph listings.

    If ``resolve_ontology_graph_uri``'s exclusion regressed, rule triples would
    leak into the Phase-2 slice and quietly inflate every VKG query's token
    budget — a cost regression with no error anywhere.

    :param neptune: The live service.
    :param seeded_rule: The rule to persist first, so there is something to leak.
    :param ontology_graph_uri: The resolved ontology graph URI.
    """
    neptune.write_rule(rule=seeded_rule)
    assert not ontology_graph_uri.endswith("/rules")
    # Ask the ontology graph itself whether the rule is in it. A subject-bound
    # SELECT is the direct question; a string search over get_ontology() would
    # only prove that the ontology PROJECTION hides rules, not the graph.
    rows = neptune.select_in_graph(
        query=(
            "SELECT ?p ?o WHERE { "
            f"?s ?p ?o . FILTER(CONTAINS(STR(?s), \"{_RULE_ID}\")) }} LIMIT 1"
        ),
        graph_uri=ontology_graph_uri,
    )
    assert rows == [], f"rule triples leaked into {ontology_graph_uri}: {rows}"


def test_all_four_validation_levels_pass_against_the_live_ontology(
    neptune: Any, seeded_rule: Any, ontology_graph_uri: str,
) -> None:
    """Semantic + operational validation need the real ontology and mappings.

    :param neptune: The live service.
    :param seeded_rule: The rule to validate.
    :param ontology_graph_uri: The ontology graph URI the validators query.
    """
    from agents.shared.rule_validator import validate_rule

    reports, missing = validate_rule(
        rule=seeded_rule,
        ask=neptune.ask,
        select=neptune.select_in_graph,
        ontology_graph_uri=ontology_graph_uri,
    )
    for report in reports:
        print(f"{report.level}: passed={report.passed} "
              f"violations={report.violations}")
    assert all(r.passed for r in reports), "seeded rule failed validation"
    # A non-empty missing list is not a test failure in production — it routes to
    # advisory — but here it means the IRIs supplied by env are not data-mapped,
    # so the Ontop assertions below would never run.
    assert missing == [], (
        f"unmapped bindings {missing} — pick a GOVERNED_RULES_E2E_BINDING that "
        "has an OBDA column mapping, or the publish test proves nothing"
    )


def test_publish_smoke_executes_the_compiled_sparql_through_ontop(
    published_rule: Any,
) -> None:
    """Ontop reformulated the compiled rule and Athena ran the result.

    :param published_rule: The output of the real publish gate.
    """
    assert published_rule.lifecycle == "PUBLISHED"
    # Non-empty compiled SPARQL distinguishes the evaluable branch from the
    # advisory one: the advisory branch also returns PUBLISHED, with the SPARQL
    # deliberately empty, and would sail past a lifecycle-only assertion.
    assert (published_rule.compiled_sparql or "").strip()
    assert published_rule.missing_inputs == []
    print(f"compiled SPARQL:\n{published_rule.compiled_sparql}")


def test_knn_hydration_from_a_live_sparql_select(
    neptune: Any, published_rule: Any,
) -> None:
    """Hydration reads Neptune directly — there is no DDB projection to fake.

    :param neptune: The live service.
    :param published_rule: Ensures a PUBLISHED rule exists to index.
    """
    from agents.shared import knn_hydration

    knn_hydration.reset_for_tests()
    indexed = knn_hydration.hydrate_rules_namespace(
        namespace=_ONTOLOGY_ID,
        # hydrate_rules_namespace reads rows with .get(), but read_rules returns
        # MODELS — the same model_dump bridge the query agent's
        # _fetch_published_rules uses. Passing models straight through raises
        # AttributeError inside the hydration loop.
        fetch_rules=lambda ns: [
            r.model_dump() for r in neptune.read_rules(namespace=ns)
        ],
    )
    print(f"hydrated {indexed} published rule(s) for {_ONTOLOGY_ID}")
    assert indexed >= 1
