"""Selection logic for the threshold-IRI migration.

The migration rewrites live governed rules, so the parts that decide *which*
rules to touch — and the call that builds the rewrite — are worth covering
without AWS. The AWS-facing functions are not tested here; these are the pure
ones, plus a regression for the rewrite call itself, which shipped with a wrong
keyword and failed only once the script was run with ``--apply``.
"""
from rdflib import Graph, URIRef

from agents.shared.rule_graph import (
    rule_to_graph, rules_graph_uri, rule_to_upsert_update, threshold_uri,
)
from agents.shared.rule_models import (
    Condition, Conclusion, Rule, RuleScope, Threshold,
)
from agents.shared.rule_vocab import VKGR
from scripts.migrate_threshold_iris import (
    legacy_threshold_nodes, rules_needing_migration,
)

NAMESPACE = "ont-1"
THRESHOLD_ID = "THR-1"


def _rule(*, rule_id: str) -> Rule:
    """Build a rule with one condition bound to a threshold.

    :param rule_id: Identifier for the rule.
    :returns: The rule.
    """
    return Rule(
        rule_id=rule_id, namespace=NAMESPACE, name=f"n-{rule_id}",
        description="d", rule_type="diagnostic", level="general",
        scope=RuleScope(subject_class="https://ex.org/vkg#Holding"),
        conditions=[Condition(binding="https://ex.org/vkg#days",
                              operator=">", threshold_id=THRESHOLD_ID)],
        conclusion=Conclusion(outcome="https://ex.org/vkg#LapseRisk"),
        thresholds=[Threshold(threshold_id=THRESHOLD_ID, value=90.0,
                              unit="days", source_span="after 90 days")],
    )


def _legacy_graph(*, rule_id: str) -> Graph:
    """Serialize a rule the way the pre-fix code did, with an unscoped IRI.

    :param rule_id: Rule to serialize.
    :returns: A graph using the legacy single-segment threshold IRI.
    """
    scoped = threshold_uri(rule_id=rule_id, threshold_id=THRESHOLD_ID)
    legacy = URIRef(f"{VKGR}{THRESHOLD_ID}")
    out = Graph()
    for subject, predicate, obj in rule_to_graph(_rule(rule_id=rule_id)):
        out.add((legacy if subject == scoped else subject, predicate,
                 legacy if obj == scoped else obj))
    return out


def test_legacy_nodes_are_flagged_and_scoped_ones_are_not() -> None:
    """The scan must not report an already-migrated graph as needing work."""
    assert legacy_threshold_nodes(graph=_legacy_graph(rule_id="RULE-A")) == [
        URIRef(f"{VKGR}{THRESHOLD_ID}")
    ]
    scoped = rule_to_graph(_rule(rule_id="RULE-A"))
    assert legacy_threshold_nodes(graph=scoped) == []


def test_only_unscoped_rules_are_selected() -> None:
    """Selecting an already-scoped rule would rewrite it for no reason."""
    legacy = _legacy_graph(rule_id="RULE-A")
    stale = rules_needing_migration(graph=legacy, rules=[_rule(rule_id="RULE-A")])
    assert [r.rule_id for r in stale] == ["RULE-A"]

    scoped = rule_to_graph(_rule(rule_id="RULE-A"))
    assert rules_needing_migration(
        graph=scoped, rules=[_rule(rule_id="RULE-A")]
    ) == []


def test_migration_rewrite_targets_the_graph_it_was_given() -> None:
    """Regression: this call shipped as ``namespace=`` and raised TypeError.

    The dry run did not build the update, so the defect surfaced only under
    ``--apply``. The dry run now builds every update for exactly this reason.
    """
    graph_uri = rules_graph_uri(namespace=NAMESPACE)
    update = rule_to_upsert_update(rule=_rule(rule_id="RULE-A"),
                                   graph_uri=graph_uri)
    assert graph_uri in update
    # Delete-then-insert, or the old unscoped node would survive the rewrite.
    assert "DELETE" in update.upper()
    assert "INSERT DATA" in update.upper()
    assert str(threshold_uri(rule_id="RULE-A",
                             threshold_id=THRESHOLD_ID)) in update
