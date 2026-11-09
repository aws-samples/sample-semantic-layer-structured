"""Rewrite pre-fix rules so their threshold nodes are scoped to their rule.

Before the threshold-IRI scoping fix, a threshold node was named
``vkgr:<threshold_id>`` — global, shared by every rule that picked the same id.
Because ``vkgr:threshold`` is in the rule-delete child closure, deleting any one
of those rules deleted the node its siblings referenced, and the survivors then
failed to deserialize, returning 500 for the whole namespace's rule list.

The serializer fix only takes effect when a rule is next written, so graphs
written before it keep the hazard until migrated. This script does that
migration by re-persisting each rule from its own in-memory model, which
preserves every field — crucially ``lifecycle`` and ``compiled_sparql``, which
the REST API's own create/update paths deliberately reset.

ORDERING IS LOAD-BEARING: every rule is read BEFORE any rule is written. Two
legacy rules sharing a threshold id share one node, so upserting the first would
destroy the second's threshold before it had been captured. Reading everything
up front means each rule is rewritten from a model taken while the graph was
still intact.

Dry run by default; pass ``--apply`` to write.

Usage::

    python3 scripts/migrate_threshold_iris.py --namespace <ontology_id>
    python3 scripts/migrate_threshold_iris.py --namespace <ontology_id> --apply
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import boto3
from rdflib import Graph, Literal, URIRef

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.shared.rule_graph import (  # noqa: E402
    graph_to_rules, rules_graph_uri, rule_to_upsert_update, threshold_uri,
)
from agents.shared.rule_models import Rule  # noqa: E402
from agents.shared.rule_vocab import VKGR  # noqa: E402

NEPTUNE_TOOLS_FUNCTION = 'semantic-layer-dev-neptune-tools-v2'
_THRESHOLD_SEGMENT = 'threshold'


def _invoke(*, client: Any, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke one neptune-tools tool.

    The AgentCore gateway convention puts the tool name in the Lambda client
    context, not the event; the event is a flat map of the tool's arguments.

    :param client: A boto3 Lambda client.
    :param tool: Tool name, e.g. ``execute_sparql_query``.
    :param arguments: The tool's arguments.
    :returns: The parsed response payload.
    :raises RuntimeError: If the handler errored or returned a non-200 status,
        so a failed write is never mistaken for a successful one.
    """
    response = client.invoke(
        FunctionName=NEPTUNE_TOOLS_FUNCTION,
        Payload=json.dumps(arguments).encode(),
        ClientContext=base64.b64encode(
            json.dumps({'custom': {'bedrockAgentCoreToolName': tool}}).encode()
        ).decode(),
    )
    payload = json.loads(response['Payload'].read())
    if 'errorMessage' in payload:
        raise RuntimeError(f'{tool} failed: {payload["errorMessage"]}')
    if payload.get('statusCode') != 200:
        raise RuntimeError(f'{tool} returned {payload.get("statusCode")}: '
                           f'{str(payload.get("body"))[:400]}')
    return payload


def read_rules_graph(*, client: Any, namespace: str) -> Graph:
    """Load a namespace's whole rules graph into memory.

    :param client: A boto3 Lambda client.
    :param namespace: Ontology id owning the rules graph.
    :returns: An rdflib graph holding every triple in the rules graph.
    :raises RuntimeError: If the SPARQL read fails.
    """
    graph_uri = rules_graph_uri(namespace=namespace)
    payload = _invoke(client=client, tool='execute_sparql_query', arguments={
        'sparql_query': (
            f'SELECT ?s ?p ?o WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}'
        ),
        'query_type': 'SELECT',
    })
    graph = Graph()
    for binding in json.loads(payload['body'])['results']['bindings']:
        graph.add((
            _term(binding['s']), _term(binding['p']), _term(binding['o']),
        ))
    return graph


def _term(binding: Dict[str, str]) -> Any:
    """Convert one SPARQL JSON result term to an rdflib term.

    :param binding: A SPARQL JSON results term object.
    :returns: The rdflib term.
    :raises ValueError: On an unrecognised term type, rather than guessing.
    """
    kind = binding['type']
    if kind == 'uri':
        return URIRef(binding['value'])
    if kind == 'bnode':
        # Reuse the server's label so the rule's closure stays connected.
        from rdflib import BNode
        return BNode(binding['value'])
    if kind in ('literal', 'typed-literal'):
        datatype = binding.get('datatype')
        return Literal(
            binding['value'],
            datatype=URIRef(datatype) if datatype else None,
            lang=binding.get('xml:lang'),
        )
    raise ValueError(f'unrecognised SPARQL term type: {kind!r}')


def legacy_threshold_nodes(*, graph: Graph) -> List[URIRef]:
    """List threshold nodes still using the unscoped IRI shape.

    A scoped IRI has the form ``vkgr:<rule_id>/threshold/<threshold_id>``; the
    legacy shape is a single segment.

    :param graph: The rules graph.
    :returns: Distinct legacy threshold node IRIs, sorted for stable output.
    """
    legacy = set()
    for obj in graph.objects(None, VKGR.threshold):
        local = str(obj)[len(str(VKGR)):] if str(obj).startswith(str(VKGR)) \
            else str(obj)
        parts = local.split('/')
        if not (len(parts) == 3 and parts[1] == _THRESHOLD_SEGMENT):
            legacy.add(obj)
    return sorted(legacy)


def rules_needing_migration(
    *, graph: Graph, rules: List[Rule],
) -> List[Rule]:
    """Select rules whose threshold links are not yet rule-scoped.

    :param graph: The rules graph as read.
    :param rules: Every rule recovered from that graph.
    :returns: The subset to rewrite.
    """
    stale = []
    for rule in rules:
        subject = URIRef(f'{VKGR}{rule.rule_id}')
        expected = {
            threshold_uri(rule_id=rule.rule_id,
                          threshold_id=threshold.threshold_id)
            for threshold in rule.thresholds
        }
        expected |= {
            threshold_uri(rule_id=rule.rule_id,
                          threshold_id=condition.threshold_id)
            for condition in rule.conditions
            if condition.threshold_id is not None
        }
        linked = set(graph.objects(subject, VKGR.threshold))
        for condition_node in graph.objects(subject, VKGR.hasCondition):
            linked |= set(graph.objects(condition_node, VKGR.threshold))
        if linked - expected:
            stale.append(rule)
    return stale


def migrate(*, client: Any, namespace: str, apply: bool) -> Tuple[int, int]:
    """Migrate one namespace's rules graph.

    :param client: A boto3 Lambda client.
    :param namespace: Ontology id owning the rules graph.
    :param apply: When False, report only and write nothing.
    :returns: ``(rules_total, rules_migrated)``.
    :raises RuntimeError: If a rule does not survive the read round-trip, since
        rewriting from a lossy model would silently drop governance data.
    """
    graph = read_rules_graph(client=client, namespace=namespace)
    rules = graph_to_rules(graph)
    print(f'  read {len(rules)} rule(s), {len(graph)} triple(s)')

    legacy = legacy_threshold_nodes(graph=graph)
    if legacy:
        print(f'  legacy threshold nodes: '
              f'{[str(n).split("#")[-1] for n in legacy]}')

    stale = rules_needing_migration(graph=graph, rules=rules)
    if not stale:
        print('  nothing to migrate — all threshold IRIs already scoped')
        return len(rules), 0

    graph_uri = rules_graph_uri(namespace=namespace)
    updates = []
    for rule in stale:
        # Guard the read before trusting it as the source for a rewrite.
        if not rule.thresholds and not any(
            c.threshold_id for c in rule.conditions
        ):
            raise RuntimeError(
                f'{rule.rule_id} was selected for migration but carries no '
                f'threshold — refusing to rewrite it'
            )
        # Build every update NOW, even for a dry run: a dry run that only
        # printed would leave the write path completely unexercised, so a defect
        # in query construction would first surface mid-migration with some
        # rules already rewritten. Target the graph we actually read rather than
        # letting the helper re-derive it from the rule's own namespace field.
        updates.append(rule_to_upsert_update(rule=rule, graph_uri=graph_uri))
        print(f'  {"rewriting" if apply else "would rewrite"} {rule.rule_id} '
              f'(lifecycle={rule.lifecycle}, '
              f'compiled={"yes" if rule.compiled_sparql else "no"}, '
              f'thresholds={[t.threshold_id for t in rule.thresholds]})')

    if not apply:
        print(f'  built {len(updates)} update(s) successfully')
        print('  DRY RUN — pass --apply to write')
        return len(rules), 0

    for update in updates:
        _invoke(client=client, tool='execute_sparql_query', arguments={
            'sparql_query': update,
            'query_type': 'UPDATE',
        })

    # Verify against the graph as it now stands, not against what we hoped.
    after_graph = read_rules_graph(client=client, namespace=namespace)
    after = {r.rule_id: r for r in graph_to_rules(after_graph)}
    before = {r.rule_id: r for r in rules}
    if set(after) != set(before):
        raise RuntimeError(f'rule set changed: before={sorted(before)} '
                           f'after={sorted(after)}')
    for rule_id, original in before.items():
        if after[rule_id].model_dump() != original.model_dump():
            raise RuntimeError(f'{rule_id} did not survive the rewrite intact')
    remaining = legacy_threshold_nodes(graph=after_graph)
    if remaining:
        raise RuntimeError(f'legacy threshold nodes remain: {remaining}')
    print(f'  verified: {len(after)} rule(s) intact, all threshold IRIs scoped')
    return len(rules), len(stale)


def main() -> int:
    """Parse arguments and migrate each requested namespace.

    :returns: 0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', action='append', required=True,
                        help='Ontology id to migrate; repeatable.')
    parser.add_argument('--apply', action='store_true',
                        help='Write the migration (default is a dry run).')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    client = boto3.client('lambda', region_name=args.region)
    migrated = 0
    for namespace in args.namespace:
        print(f'{namespace}:')
        _total, count = migrate(
            client=client, namespace=namespace, apply=args.apply,
        )
        migrated += count
    print(f'\n{migrated} rule(s) migrated')
    return 0


if __name__ == '__main__':
    sys.exit(main())
