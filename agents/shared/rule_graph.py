"""RDF (de)serialization for governed rules — RDF is the neutral IR.

Design §3: a rule is stored as ``vkgr:`` triples, not as JSON. This module is
the ONLY place that builds rule SPARQL, so the REST Lambda and the agent
runtime read and write rules with one implementation (the REST Lambda's
``NeptuneService`` methods are thin adapters over these functions).

Split deliberately:

* ``rule_to_graph`` / ``graph_to_rules`` are pure — no AWS, no endpoint — so
  the round-trip is unit-testable and the same code validates in both runtimes.
* The SPARQL wrappers take an injected ``execute_sparql`` collaborator
  (matching ``NeptuneConstruct``'s style) so tests never touch Neptune.

Ordering note: RDF is unordered, but compiled SPARQL clause order must be
deterministic across reloads. Node-valued lists (conditions, test cases) carry
an explicit integer index; string lists (synonyms, missing inputs) are stored
as RDF Collections, which preserve order natively and match the
``vkgr:missingInput ()`` shape in design §3.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF

from .rule_models import (
    Condition, Conclusion, Provenance, Rule, RuleScope, TestCase, Threshold,
)
from .rule_vocab import RULES_GRAPH_SUFFIX, VKGR

# Predicates that link a rule to a child node. Enumerated because SPARQL has no
# wildcard property path: the delete update walks exactly these to collect a
# rule's closure.
#
# A zero-or-one path over these predicates is the COMPLETE closure, but not
# because the tree is one level deep — a condition also carries a
# ``vkgr:threshold`` link to the threshold node. It is complete because that
# threshold is the only grandchild and the rule links to it directly as well, so
# the depth-1 walk still reaches it. Every other child holds nothing but
# literals and IRIs the rule does not own (e.g. a test case's subject class).
#
# This closure is only SAFE because every child node is owned exclusively by
# this rule: the bnodes are unreachable from anywhere else, and threshold IRIs
# are rule-scoped by ``threshold_uri``. Adding a child link to a node that other
# rules can reach would make deleting one rule corrupt its siblings.
_CHILD_LINKS = (
    "scope", "hasCondition", "conclusion", "threshold", "derivedFrom",
    "hasTestCase",
)

_DEFAULT_GRAPH_URI_PREFIX = "https://semantic-layer.aws/ontologies/"


def default_graph_uri_prefix() -> str:
    """Read the ontology graph URI prefix from the environment.

    Same variable and default as ``neptune_metadata._graph_uri`` so rules land
    beside the ontology they govern.

    :returns: The configured prefix, with a trailing slash by convention.
    """
    return os.environ.get("GRAPH_URI_PREFIX", _DEFAULT_GRAPH_URI_PREFIX)


def rules_graph_uri(*, namespace: str, graph_uri_prefix: Optional[str] = None) -> str:
    """Build the sibling rules-graph URI for an ontology namespace.

    :param namespace: Ontology id (the UUID the ontology graph is keyed by).
    :param graph_uri_prefix: Prefix override; defaults to the environment.
    :returns: ``<prefix><namespace>/rules``.
    """
    prefix = graph_uri_prefix if graph_uri_prefix is not None else default_graph_uri_prefix()
    return f"{prefix}{namespace}{RULES_GRAPH_SUFFIX}"


def _resolve_graph_uri(
    *, graph_uri: Optional[str], namespace: Optional[str],
) -> str:
    """Accept either an explicit rules-graph URI or a namespace to derive one.

    Callers differ: the shared modules pass ``graph_uri`` directly, while the
    REST Lambda's service layer only knows the namespace. Fails loudly rather
    than defaulting, because a wrong graph URI would write rule triples into
    the ontology graph.

    :param graph_uri: Explicit rules-graph URI, or None.
    :param namespace: Ontology id to derive the URI from, or None.
    :returns: The rules-graph URI.
    :raises ValueError: If neither argument was supplied.
    """
    if graph_uri:
        return graph_uri
    if namespace:
        return rules_graph_uri(namespace=namespace)
    raise ValueError("one of graph_uri or namespace is required")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _add_string_list(
    graph: Graph, subject: URIRef, predicate: URIRef, values: List[str],
) -> None:
    """Attach an ordered string list to a subject as an RDF Collection.

    A Collection (rather than repeated triples) because order must survive the
    round-trip: ``missing_inputs`` order is what the admin UI and the persisted
    rule agree on, and repeated triples come back in arbitrary order.

    :param graph: Graph to mutate.
    :param subject: Node the list hangs off.
    :param predicate: Linking predicate.
    :param values: Ordered string values; an empty list becomes ``rdf:nil``.
    """
    head = BNode()
    Collection(graph, head, [Literal(v) for v in values])
    # An empty Collection leaves `head` unattached and unused; rdf:nil is the
    # correct term for the empty list (design §3 writes `vkgr:missingInput ()`).
    graph.add((subject, predicate, head if values else RDF.nil))


def _read_string_list(
    graph: Graph, subject: URIRef, predicate: URIRef,
) -> List[str]:
    """Read back an ordered string list written by :func:`_add_string_list`.

    :param graph: Graph to read.
    :param subject: Node the list hangs off.
    :param predicate: Linking predicate.
    :returns: The values in their original order; empty when absent or nil.
    """
    head = graph.value(subject, predicate)
    if head is None or head == RDF.nil:
        return []
    return [str(item) for item in Collection(graph, head)]


_THRESHOLD_SEGMENT = "threshold"


def threshold_uri(*, rule_id: str, threshold_id: str) -> URIRef:
    """Build the rule-scoped IRI naming one threshold node.

    Thresholds are named IRIs rather than blank nodes so a reviewer can address
    a bound directly, but the IRI MUST be qualified by its owning rule. Keying
    it on ``threshold_id`` alone gives every rule that picks the same id — and
    ``THR-1`` is an obvious collision — one shared node. Because
    ``vkgr:threshold`` is in :data:`_CHILD_LINKS`, deleting any one of those
    rules then deletes the node its siblings still reference; the survivors fail
    to deserialize and take the whole namespace's rule list down with them.

    Both segments are percent-encoded, which keeps the IRI parseable back to the
    id by :func:`threshold_id_from_uri` and keeps it a legal IRI even when an id
    contains a space or a slash (neither model field constrains its charset).

    :param rule_id: Identifier of the rule that owns the threshold.
    :param threshold_id: Identifier of the threshold itself.
    :returns: The threshold node's IRI.
    """
    return URIRef(
        f"{VKGR}{quote(rule_id, safe='')}"
        f"/{_THRESHOLD_SEGMENT}/{quote(threshold_id, safe='')}"
    )


def threshold_id_from_uri(*, term: Any) -> str:
    """Recover a threshold id from its node IRI.

    The id has to be read out of the IRI rather than the node's
    ``vkgr:thresholdId`` literal because a condition is allowed to reference a
    threshold the source document never stated (the rule miner leaves such a
    reference dangling on purpose, and the rule records it under
    ``missing_inputs``). That node carries no triples at all, so the IRI is the
    only surviving carrier of the id.

    Both the rule-scoped shape written by :func:`threshold_uri` and the legacy
    unscoped ``vkgr:<threshold_id>`` shape are accepted, so rules persisted
    before this fix still read back. Legacy nodes keep the sharing hazard until
    their rule is next written; they are not silently repaired here, because
    rewriting a rule as a side effect of reading it would be worse.

    :param term: The threshold node IRI.
    :returns: The threshold id.
    """
    local = _strip_vkgr(term)
    parts = local.split("/")
    if len(parts) == 3 and parts[1] == _THRESHOLD_SEGMENT:
        return unquote(parts[2])
    return local


def rule_to_graph(rule: Rule) -> Graph:
    """Serialize one rule to ``vkgr:`` triples.

    The rule is a named IRI (``vkgr:RULE-004``) and so is each of its
    thresholds, qualified by the owning rule
    (``vkgr:RULE-004/threshold/THR-011``) — see :func:`threshold_uri` for why
    the qualification is load-bearing. Addressable bounds are what makes "never
    invent a missing threshold" auditable. Scope, conditions, conclusion,
    provenance and test cases are blank nodes: they have no identity outside
    their rule.

    :param rule: The rule to serialize.
    :returns: A fresh graph containing only this rule's triples.
    """
    g = Graph()
    g.bind("vkgr", VKGR)
    s = URIRef(f"{VKGR}{rule.rule_id}")

    g.add((s, RDF.type, VKGR.Rule))
    g.add((s, VKGR.ruleId, Literal(rule.rule_id)))
    g.add((s, VKGR.namespace, Literal(rule.namespace)))
    g.add((s, VKGR.name, Literal(rule.name)))
    g.add((s, VKGR.description, Literal(rule.description)))
    g.add((s, VKGR.ruleType, Literal(rule.rule_type)))
    g.add((s, VKGR.level, Literal(rule.level)))
    g.add((s, VKGR.lifecycle, Literal(rule.lifecycle)))
    g.add((s, VKGR.version, Literal(rule.version)))

    # Scope
    scope = BNode()
    g.add((s, VKGR.scope, scope))
    g.add((scope, VKGR.subjectClass, URIRef(rule.scope.subject_class)))
    if rule.scope.process_stage is not None:
        g.add((scope, VKGR.processStage, Literal(rule.scope.process_stage)))

    # Conditions — the explicit index is what makes clause order recoverable.
    for index, condition in enumerate(rule.conditions):
        node = BNode()
        g.add((s, VKGR.hasCondition, node))
        g.add((node, VKGR.conditionIndex, Literal(index)))
        g.add((node, VKGR.binding, URIRef(condition.binding)))
        g.add((node, VKGR.operator, Literal(condition.operator)))
        if condition.threshold_id is not None:
            # Same builder as the threshold node itself, or the condition would
            # point at an IRI nothing defines.
            g.add((node, VKGR.threshold, threshold_uri(
                rule_id=rule.rule_id, threshold_id=condition.threshold_id,
            )))
        else:
            # Literal() picks the xsd datatype from the Python type, so bool /
            # float / str all round-trip to the same type they went in as.
            g.add((node, VKGR.value, Literal(condition.value)))

    # Conclusion — epistemicStatus is an IRI from the closed set, so the SHACL
    # sh:in shape can constrain it the same way enum_shapes.py does.
    conclusion = BNode()
    g.add((s, VKGR.conclusion, conclusion))
    g.add((conclusion, VKGR.outcome, URIRef(rule.conclusion.outcome)))
    g.add((conclusion, VKGR.epistemicStatus,
           URIRef(f"{VKGR}{rule.conclusion.epistemic_status}")))
    if rule.conclusion.attested_by is not None:
        g.add((conclusion, VKGR.attestedBy, Literal(rule.conclusion.attested_by)))
    if rule.conclusion.attestation_note is not None:
        g.add((conclusion, VKGR.attestationNote,
               Literal(rule.conclusion.attestation_note)))

    # Thresholds
    for threshold in rule.thresholds:
        node = threshold_uri(
            rule_id=rule.rule_id, threshold_id=threshold.threshold_id,
        )
        g.add((s, VKGR.threshold, node))
        g.add((node, RDF.type, VKGR.Threshold))
        g.add((node, VKGR.thresholdId, Literal(threshold.threshold_id)))
        g.add((node, VKGR.value, Literal(threshold.value)))
        g.add((node, VKGR.unit, Literal(threshold.unit)))
        g.add((node, VKGR.sourceSpan, Literal(threshold.source_span)))
        if threshold.approved_by is not None:
            g.add((node, VKGR.approvedBy, Literal(threshold.approved_by)))

    # Provenance
    if rule.provenance is not None:
        node = BNode()
        g.add((s, VKGR.derivedFrom, node))
        g.add((node, RDF.type, VKGR.Provenance))
        g.add((node, VKGR.documentId, Literal(rule.provenance.document_id)))
        g.add((node, VKGR.sourceSentence, Literal(rule.provenance.source_sentence)))
        g.add((node, VKGR.modelId, Literal(rule.provenance.model_id)))
        g.add((node, VKGR.confidence, Literal(rule.provenance.confidence)))
        if rule.provenance.page is not None:
            g.add((node, VKGR.page, Literal(rule.provenance.page)))

    # Test cases
    for index, case in enumerate(rule.test_cases):
        node = BNode()
        g.add((s, VKGR.hasTestCase, node))
        g.add((node, VKGR.testCaseIndex, Literal(index)))
        g.add((node, VKGR.kind, Literal(case.kind)))
        g.add((node, VKGR.subjectClass, URIRef(case.subject_class)))
        g.add((node, VKGR.expectedFires, Literal(case.expected_fires)))
        g.add((node, VKGR.rationale, Literal(case.rationale)))
        # One JSON literal rather than a node per entry: `inputs` is an opaque
        # binding→value map nothing queries by key, so per-entry nodes would buy
        # queryability no caller wants and cost a lossy round-trip on value
        # types. sort_keys keeps the serialization byte-stable.
        g.add((node, VKGR.inputsJson,
               Literal(json.dumps(case.inputs, sort_keys=True))))

    _add_string_list(g, s, VKGR.synonym, rule.synonyms)
    _add_string_list(g, s, VKGR.missingInput, rule.missing_inputs)

    if rule.specialises is not None:
        g.add((s, VKGR.specialises, Literal(rule.specialises)))
    if rule.reviewed_by is not None:
        g.add((s, VKGR.reviewedBy, Literal(rule.reviewed_by)))
    if rule.compiled_sparql is not None:
        g.add((s, VKGR.compiledSparql, Literal(rule.compiled_sparql)))

    return g


# ---------------------------------------------------------------------------
# Deserialization
# ---------------------------------------------------------------------------


class RuleReadError(Exception):
    """A stored rule could not be reconstructed from its triples.

    Deliberately NOT a subclass of ``ValueError``: the REST routers map
    ``ValueError`` to 422, and a rule that is already in the graph but cannot be
    read is a server-side data fault, not bad client input. Answering 422 would
    tell the caller to fix a payload they never sent.

    Carries ``rule_id`` so a caller can report which rule failed, or match the
    failure against a requested id, without parsing the message.
    """

    def __init__(self, message: str, *, rule_id: Optional[str] = None) -> None:
        """Build the error.

        :param message: Human-readable description naming the missing field.
        :param rule_id: Rule the failure belongs to, when it is known — it is
            not, when the missing field is ``vkgr:ruleId`` itself.
        """
        super().__init__(message)
        self.rule_id = rule_id


def _required(
    graph: Graph,
    subject: Any,
    predicate: URIRef,
    *,
    rule_id: Optional[str],
    where: Optional[str] = None,
) -> Any:
    """Read a predicate that ``rule_to_graph`` always writes.

    Every required field goes through here rather than through
    ``str(graph.value(...))``. ``str(None)`` returns the literal string
    ``"None"``, so the old form stored a missing value as if it were data — a
    threshold could ship with ``unit="None"`` and ``source_span="None"``, and
    ``source_span`` carries ``min_length=1`` precisely to make an invented bound
    impossible.

    Child-node links (``vkgr:scope``, ``vkgr:conclusion``, ...) must also come
    through here, not just literals: rdflib treats ``graph.value(None, p)`` as a
    wildcard over subjects, so reading a field off a node that turned out to be
    absent would silently pick up SOME OTHER rule's value.

    :param graph: Graph to read.
    :param subject: Subject node holding the predicate.
    :param predicate: Predicate to read.
    :param rule_id: Rule being read, for the error message.
    :param where: Optional description of the containing node, e.g.
        ``"threshold THR-1"``, so a failure points at one child of many.
    :returns: The raw rdflib term.
    :raises RuleReadError: If the predicate is absent.
    """
    value = graph.value(subject, predicate)
    if value is None:
        raise RuleReadError(
            f"rule {rule_id or '<unidentified>'}: "
            f"{where + ' is ' if where else ''}"
            f"missing required {_strip_vkgr(predicate)} (<{subject}>)",
            rule_id=rule_id,
        )
    return value


def _required_str(
    graph: Graph,
    subject: Any,
    predicate: URIRef,
    *,
    rule_id: Optional[str],
    where: Optional[str] = None,
) -> str:
    """Read a required literal as ``str``.

    :param graph: Graph to read.
    :param subject: Subject node.
    :param predicate: Predicate to read.
    :param rule_id: Rule being read, for the error message.
    :param where: Optional containing-node description.
    :returns: The value.
    :raises RuleReadError: If the predicate is absent.
    """
    return str(_required(graph, subject, predicate,
                         rule_id=rule_id, where=where))


def _required_typed(
    graph: Graph,
    subject: Any,
    predicate: URIRef,
    *,
    caster: Callable[[Any], Any],
    rule_id: Optional[str],
    where: Optional[str] = None,
) -> Any:
    """Read a required literal and convert it to a Python scalar.

    ``toPython()`` restores the type the xsd datatype encodes, so a bool comes
    back a bool rather than the string ``"true"``. A literal that is present but
    not convertible is the same class of fault as a missing one — a corrupt
    stored rule — so the conversion error is re-raised as ``RuleReadError``
    rather than escaping as a bare ``ValueError`` the routers would call 422.

    :param graph: Graph to read.
    :param subject: Subject node.
    :param predicate: Predicate to read.
    :param caster: Conversion applied to the ``toPython()`` result, e.g. ``int``.
    :param rule_id: Rule being read, for the error message.
    :param where: Optional containing-node description.
    :returns: The converted value.
    :raises RuleReadError: If the predicate is absent or unconvertible.
    """
    term = _required(graph, subject, predicate, rule_id=rule_id, where=where)
    try:
        return caster(term.toPython() if hasattr(term, "toPython") else term)
    except (TypeError, ValueError) as exc:
        raise RuleReadError(
            f"rule {rule_id or '<unidentified>'}: "
            f"{where + ' has ' if where else ''}"
            f"unreadable {_strip_vkgr(predicate)} {term!r} (<{subject}>): {exc}",
            rule_id=rule_id,
        ) from exc


def _strip_vkgr(term: Any) -> str:
    """Drop the ``vkgr:`` prefix from a term, yielding its local name.

    :param term: An rdflib term in the VKGR namespace.
    :returns: The local name (unchanged if the term is not in the namespace).
    """
    text = str(term)
    prefix = str(VKGR)
    return text[len(prefix):] if text.startswith(prefix) else text


def _opt_str(graph: Graph, subject: Any, predicate: URIRef) -> Optional[str]:
    """Read an optional literal as ``str``, or None when absent.

    :param graph: Graph to read.
    :param subject: Subject node.
    :param predicate: Predicate to read.
    :returns: The value, or None.
    """
    value = graph.value(subject, predicate)
    return None if value is None else str(value)


def _condition_from_node(
    graph: Graph, node: Any, *, rule_id: Optional[str], index: int,
) -> Condition:
    """Rebuild one condition from its blank node.

    :param graph: Graph holding the condition.
    :param node: The condition node.
    :param rule_id: Owning rule, for error messages.
    :param index: The condition's position, so an error names which one failed.
    :returns: The reconstructed condition.
    :raises RuleReadError: If a required field is missing.
    """
    where = f"condition {index}"
    kwargs: Dict[str, Any] = {
        "binding": _required_str(graph, node, VKGR.binding,
                                 rule_id=rule_id, where=where),
        "operator": _required_str(graph, node, VKGR.operator,
                                  rule_id=rule_id, where=where),
    }
    threshold_ref = graph.value(node, VKGR.threshold)
    if threshold_ref is not None:
        kwargs["threshold_id"] = threshold_id_from_uri(term=threshold_ref)
    else:
        # A condition carries EITHER a threshold reference or an inline value, so
        # the value is required only on this branch. `identity` keeps whatever
        # Python type the xsd datatype decoded to.
        kwargs["value"] = _required_typed(
            graph, node, VKGR.value, caster=lambda v: v,
            rule_id=rule_id, where=where,
        )
    return Condition(**kwargs)


def _rule_from_subject(graph: Graph, subject: URIRef) -> Rule:
    """Rebuild one rule from its subject IRI.

    :param graph: Graph holding the rule's closure.
    :param subject: The rule's IRI.
    :returns: The reconstructed rule.
    :raises RuleReadError: If any field the serializer always writes is absent,
        naming the rule and the field rather than failing as an opaque
        ``AttributeError`` on ``None``.
    """
    # Read the id FIRST so every later error can name the rule it belongs to.
    rule_id = str(_required(graph, subject, VKGR.ruleId, rule_id=None))

    scope_node = _required(graph, subject, VKGR.scope, rule_id=rule_id)
    scope = RuleScope(
        subject_class=_required_str(graph, scope_node, VKGR.subjectClass,
                                    rule_id=rule_id, where="scope"),
        **(
            {"process_stage": str(graph.value(scope_node, VKGR.processStage))}
            if graph.value(scope_node, VKGR.processStage) is not None
            else {}
        ),
    )

    condition_nodes = sorted(
        graph.objects(subject, VKGR.hasCondition),
        key=lambda n: _required_typed(graph, n, VKGR.conditionIndex,
                                      caster=int, rule_id=rule_id,
                                      where="condition"),
    )
    conditions = [
        _condition_from_node(graph, n, rule_id=rule_id, index=i)
        for i, n in enumerate(condition_nodes)
    ]

    conclusion_node = _required(graph, subject, VKGR.conclusion,
                                rule_id=rule_id)
    attestation: Dict[str, Any] = {}
    for field, predicate in (
        ("attested_by", VKGR.attestedBy),
        ("attestation_note", VKGR.attestationNote),
    ):
        value = _opt_str(graph, conclusion_node, predicate)
        if value is not None:
            attestation[field] = value
    conclusion = Conclusion(
        outcome=_required_str(graph, conclusion_node, VKGR.outcome,
                              rule_id=rule_id, where="conclusion"),
        # `epistemic_status` has a model default, but the serializer always
        # writes it, so a graph that omits it is corrupt rather than old —
        # defaulting here would quietly relabel a rule's epistemic standing.
        epistemic_status=_strip_vkgr(
            _required(graph, conclusion_node, VKGR.epistemicStatus,
                      rule_id=rule_id, where="conclusion")
        ),
        **attestation,
    )

    thresholds: List[Threshold] = []
    for node in graph.objects(subject, VKGR.threshold):
        # Name the threshold from its IRI, not from vkgr:thresholdId — the id
        # predicate is itself one of the fields that can be missing, and the
        # rule-scoped IRI still carries the id even on a node stripped of triples.
        where = f"threshold {threshold_id_from_uri(term=node)}"
        approved_by = _opt_str(graph, node, VKGR.approvedBy)
        thresholds.append(Threshold(
            threshold_id=_required_str(graph, node, VKGR.thresholdId,
                                       rule_id=rule_id, where=where),
            value=_required_typed(graph, node, VKGR.value, caster=float,
                                  rule_id=rule_id, where=where),
            unit=_required_str(graph, node, VKGR.unit,
                               rule_id=rule_id, where=where),
            source_span=_required_str(graph, node, VKGR.sourceSpan,
                                      rule_id=rule_id, where=where),
            **({"approved_by": approved_by} if approved_by is not None else {}),
        ))
    # Threshold order is not semantically meaningful, but it must be stable or
    # the round-trip comparison and the admin UI list would reshuffle.
    thresholds.sort(key=lambda t: t.threshold_id)

    provenance: Optional[Provenance] = None
    provenance_node = graph.value(subject, VKGR.derivedFrom)
    if provenance_node is not None:
        # The provenance node is optional, but once present all four of its
        # fields are written unconditionally.
        page = graph.value(provenance_node, VKGR.page)
        provenance = Provenance(
            document_id=_required_str(graph, provenance_node, VKGR.documentId,
                                      rule_id=rule_id, where="provenance"),
            source_sentence=_required_str(
                graph, provenance_node, VKGR.sourceSentence,
                rule_id=rule_id, where="provenance",
            ),
            model_id=_required_str(graph, provenance_node, VKGR.modelId,
                                   rule_id=rule_id, where="provenance"),
            confidence=_required_typed(
                graph, provenance_node, VKGR.confidence, caster=float,
                rule_id=rule_id, where="provenance",
            ),
            **({"page": int(page.toPython())} if page is not None else {}),
        )

    case_nodes = sorted(
        graph.objects(subject, VKGR.hasTestCase),
        key=lambda n: _required_typed(graph, n, VKGR.testCaseIndex,
                                      caster=int, rule_id=rule_id,
                                      where="test case"),
    )
    test_cases = [
        TestCase(
            kind=_required_str(graph, n, VKGR.kind,
                               rule_id=rule_id, where=f"test case {i}"),
            subject_class=_required_str(graph, n, VKGR.subjectClass,
                                        rule_id=rule_id,
                                        where=f"test case {i}"),
            # JSONDecodeError subclasses ValueError, so malformed JSON is
            # reported as an unreadable rule like any other bad literal.
            inputs=_required_typed(graph, n, VKGR.inputsJson,
                                   caster=lambda v: json.loads(str(v)),
                                   rule_id=rule_id, where=f"test case {i}"),
            expected_fires=_required_typed(graph, n, VKGR.expectedFires,
                                           caster=bool, rule_id=rule_id,
                                           where=f"test case {i}"),
            rationale=_required_str(graph, n, VKGR.rationale,
                                    rule_id=rule_id, where=f"test case {i}"),
        )
        for i, n in enumerate(case_nodes)
    ]

    optional: Dict[str, Any] = {}
    for field, predicate in (
        ("specialises", VKGR.specialises),
        ("reviewed_by", VKGR.reviewedBy),
        ("compiled_sparql", VKGR.compiledSparql),
    ):
        value = _opt_str(graph, subject, predicate)
        if value is not None:
            optional[field] = value

    return Rule(
        rule_id=rule_id,
        namespace=_required_str(graph, subject, VKGR.namespace,
                                rule_id=rule_id),
        name=_required_str(graph, subject, VKGR.name, rule_id=rule_id),
        description=_required_str(graph, subject, VKGR.description,
                                  rule_id=rule_id),
        rule_type=_required_str(graph, subject, VKGR.ruleType,
                                rule_id=rule_id),
        level=_required_str(graph, subject, VKGR.level, rule_id=rule_id),
        scope=scope,
        conditions=conditions,
        conclusion=conclusion,
        thresholds=thresholds,
        provenance=provenance,
        synonyms=_read_string_list(graph, subject, VKGR.synonym),
        missing_inputs=_read_string_list(graph, subject, VKGR.missingInput),
        test_cases=test_cases,
        lifecycle=_required_str(graph, subject, VKGR.lifecycle,
                                rule_id=rule_id),
        version=_required_typed(graph, subject, VKGR.version, caster=int,
                                rule_id=rule_id),
        **optional,
    )


def graph_to_rules(graph: Graph) -> List[Rule]:
    """Rebuild every rule in a graph.

    :param graph: A graph containing zero or more ``vkgr:Rule`` subjects.
    :returns: The rules, ordered by ``rule_id`` for determinism; empty list
        when the graph holds no rules.
    """
    subjects = sorted(graph.subjects(RDF.type, VKGR.Rule), key=str)
    return [_rule_from_subject(graph, s) for s in subjects]


def _rule_id_for_report(graph: Graph, subject: URIRef) -> str:
    """Best-effort identifier for a rule that failed to deserialize.

    Must never raise: it runs only on the error path, and an exception here would
    replace a precise "this one rule is unreadable" report with a total failure —
    exactly the outcome ``partition_rules`` exists to prevent.

    :param graph: Graph holding the rule.
    :param subject: The rule's IRI.
    :returns: ``vkgr:ruleId`` when readable, else the IRI's local name.
    """
    try:
        stored = graph.value(subject, VKGR.ruleId)
        if stored is not None:
            return str(stored)
    except Exception:  # noqa: BLE001 - see the docstring: never raise here.
        pass
    return _strip_vkgr(subject)


def partition_rules(graph: Graph) -> Tuple[List[Rule], List[Dict[str, str]]]:
    """Rebuild every rule in a graph, isolating the ones that cannot be read.

    Companion to ``graph_to_rules``, which stays strict on purpose: the migration
    script and the agent runtime both call it, and for them skipping a rule is
    worse than raising — a migration that skips a rule leaves it unmigrated while
    reporting success.

    This exists for the list endpoint, where one corrupt rule must not hide every
    healthy one. The failures are RETURNED, not swallowed: a caller that drops
    them is back to a silent fallback.

    :param graph: A graph containing zero or more ``vkgr:Rule`` subjects.
    :returns: ``(rules, failures)`` — rules ordered by subject IRI as in
        ``graph_to_rules``, and one ``{"rule_id", "error"}`` map per unreadable
        rule.
    """
    rules: List[Rule] = []
    failures: List[Dict[str, str]] = []
    for subject in sorted(graph.subjects(RDF.type, VKGR.Rule), key=str):
        try:
            rules.append(_rule_from_subject(graph, subject))
        except Exception as exc:  # noqa: BLE001 - any fault isolates one rule.
            failures.append({
                "rule_id": _rule_id_for_report(graph, subject),
                "error": str(exc),
            })
    return rules, failures


# ---------------------------------------------------------------------------
# SPARQL wrappers
# ---------------------------------------------------------------------------


def rule_to_insert_data(
    rule: Rule, *, graph_uri: Optional[str] = None,
    namespace: Optional[str] = None,
) -> str:
    """Build an ``INSERT DATA`` update placing one rule in the rules graph.

    Literals are escaped by rdflib's N-Triples serializer rather than by
    f-stringing into SPARQL: a quote or newline in a source sentence would
    otherwise break the update (or worse, inject into it).

    :param rule: The rule to write.
    :param graph_uri: Explicit rules-graph URI.
    :param namespace: Namespace to derive the URI from, if no URI is given.
    :returns: A SPARQL UPDATE string.
    """
    target = _resolve_graph_uri(graph_uri=graph_uri, namespace=namespace)
    triples = rule_to_graph(rule).serialize(format="nt")
    return "INSERT DATA {{ GRAPH <{uri}> {{\n{triples}}} }}".format(
        uri=target, triples=triples,
    )


def delete_rule_update(
    *, rule_id: str, graph_uri: Optional[str] = None,
    namespace: Optional[str] = None,
) -> str:
    """Build a ``DELETE WHERE`` removing one rule and its child nodes.

    The zero-or-one property path over :data:`_CHILD_LINKS` matches the rule
    subject itself plus each of its direct children, which is the full closure
    (no child node links to another). Scoped to the rules graph so an
    ontology-graph triple can never be deleted by a rule edit.

    :param rule_id: Rule to remove.
    :param graph_uri: Explicit rules-graph URI.
    :param namespace: Namespace to derive the URI from, if no URI is given.
    :returns: A SPARQL UPDATE string.
    """
    target = _resolve_graph_uri(graph_uri=graph_uri, namespace=namespace)
    links = "|".join(f"<{VKGR}{name}>" for name in _CHILD_LINKS)
    # rule_id reaches here from a path parameter, so it is rendered as an IRI
    # inside angle brackets and validated by the service layer before use.
    return (
        "DELETE {{ GRAPH <{uri}> {{ ?s ?p ?o }} }} "
        "WHERE {{ GRAPH <{uri}> {{ <{rule}> ({links})? ?s . ?s ?p ?o }} }}"
    ).format(uri=target, rule=f"{VKGR}{rule_id}", links=links)


def rule_to_upsert_update(
    *, rule: Rule, graph_uri: Optional[str] = None,
) -> str:
    """Build a single update that replaces a rule: DELETE then INSERT.

    Upsert semantics matter because conditions are blank nodes. Without the
    DELETE, editing a rule would leave the previous condition nodes in the
    graph and the rule would read back with its conditions duplicated.

    :param rule: The rule to persist.
    :param graph_uri: Explicit rules-graph URI; derived from the rule's own
        namespace when omitted.
    :returns: A SPARQL UPDATE string with both operations, ``;``-separated.
    """
    target = graph_uri or rules_graph_uri(namespace=rule.namespace)
    return "{delete} ;\n{insert}".format(
        delete=delete_rule_update(rule_id=rule.rule_id, graph_uri=target),
        insert=rule_to_insert_data(rule, graph_uri=target),
    )


def _term_from_binding(cell: Dict[str, str]) -> Any:
    """Rebuild an rdflib term from one SPARQL-results JSON cell.

    :param cell: A binding cell with ``type``/``value`` (and maybe
        ``datatype`` / ``xml:lang``).
    :returns: A ``URIRef``, ``BNode`` or ``Literal``.
    """
    kind = cell.get("type")
    value = cell.get("value", "")
    if kind == "uri":
        return URIRef(value)
    if kind == "bnode":
        return BNode(value)
    datatype = cell.get("datatype")
    if datatype:
        return Literal(value, datatype=URIRef(datatype))
    lang = cell.get("xml:lang")
    return Literal(value, lang=lang) if lang else Literal(value)


def read_rules(
    *,
    namespace: str,
    execute_sparql: Callable[..., Dict[str, Any]],
    graph_uri_prefix: Optional[str] = None,
) -> List[Rule]:
    """Read every rule in a namespace's rules graph.

    Uses ``SELECT ?s ?p ?o`` and reassembles the graph from the bindings rather
    than ``CONSTRUCT``: the gateway's ``execute_sparql_query`` tool wraps
    CONSTRUCT output in a ``{"turtle": ...}`` envelope, while SELECT returns
    plain SPARQL-results JSON on every transport. One response shape is worth
    more than the marginally smaller payload.

    :param namespace: Ontology id owning the rules graph.
    :param execute_sparql: Injected executor taking ``query`` and
        ``query_type`` keywords and returning parsed SPARQL-results JSON.
    :param graph_uri_prefix: Prefix override; defaults to the environment.
    :returns: The rules in the graph; empty list when the graph is absent.
    :raises RuleReadError: If any rule in the graph cannot be read. Callers that
        must survive one corrupt rule should use ``read_rules_partitioned``.
    """
    return graph_to_rules(_read_rules_graph(
        namespace=namespace, execute_sparql=execute_sparql,
        graph_uri_prefix=graph_uri_prefix,
    ))


def read_rules_partitioned(
    *,
    namespace: str,
    execute_sparql: Callable[..., Dict[str, Any]],
    graph_uri_prefix: Optional[str] = None,
) -> Tuple[List[Rule], List[Dict[str, str]]]:
    """Read a namespace's rules, isolating any that cannot be deserialized.

    Same query as ``read_rules``; only the reassembly differs. Kept here rather
    than in the REST layer so there is still exactly one place that builds rule
    SPARQL.

    :param namespace: Ontology id owning the rules graph.
    :param execute_sparql: Injected executor taking ``query`` and
        ``query_type`` keywords and returning parsed SPARQL-results JSON.
    :param graph_uri_prefix: Prefix override; defaults to the environment.
    :returns: ``(rules, failures)`` as described on ``partition_rules``.
    """
    return partition_rules(_read_rules_graph(
        namespace=namespace, execute_sparql=execute_sparql,
        graph_uri_prefix=graph_uri_prefix,
    ))


def _read_rules_graph(
    *,
    namespace: str,
    execute_sparql: Callable[..., Dict[str, Any]],
    graph_uri_prefix: Optional[str] = None,
) -> Graph:
    """Load a namespace's rules graph into memory.

    :param namespace: Ontology id owning the rules graph.
    :param execute_sparql: Injected executor taking ``query`` and
        ``query_type`` keywords and returning parsed SPARQL-results JSON.
    :param graph_uri_prefix: Prefix override; defaults to the environment.
    :returns: The graph; empty when the rules graph is absent.
    """
    target = rules_graph_uri(
        namespace=namespace, graph_uri_prefix=graph_uri_prefix,
    )
    query = (
        "SELECT ?s ?p ?o WHERE {{ GRAPH <{uri}> {{ ?s ?p ?o }} }}"
    ).format(uri=target)
    result = execute_sparql(query=query, query_type="SELECT") or {}
    bindings = result.get("results", {}).get("bindings", [])
    graph = Graph()
    for row in bindings:
        if not {"s", "p", "o"} <= set(row):
            continue
        graph.add((
            _term_from_binding(row["s"]),
            _term_from_binding(row["p"]),
            _term_from_binding(row["o"]),
        ))
    return graph
