"""Four-level rule validation — the approval gate (design §6).

Levels, weakest to strongest:

1. **structural** — does the rule conform to the ``vkgr:`` schema? (SHACL)
2. **semantic** — do its IRIs exist in the ontology it claims to govern?
3. **operational** — is every binding mapped to a real column?
4. **epistemic** — is the claimed epistemic status backed by a human?

**Every level is FAIL-CLOSED.** This is the deliberate inverse of the
query-side contract, where a missing enum or a failed metadata lookup degrades
to a weaker answer rather than no answer. Here a validator that swallows its
own error would report "no violations" and let an unreviewed rule reach
production, which is worse than having no validator at all. So any unexpected
exception becomes ``passed=False`` with the error text as a violation.
"""
from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from rdflib import Graph

# NOT redeclared here — both constants are owned by rule_vocab. The predicate
# IRI must be exactly the one the VKG builder writes (a drifted copy would
# silently report everything unmapped), and the causal status must be exactly
# the one rule_models validates against (a drifted copy would let a causal claim
# through unattested).
from .rule_vocab import STRONGEST_EPISTEMIC_STATUS, VKG_MAPS_TO_COLUMN

if TYPE_CHECKING:  # import cycle: rule_graph imports rule_models, not vice versa
    from .rule_models import Rule

_SHAPES_RESOURCE = "rule_shapes.ttl"


@dataclass
class ValidationReport:
    """Outcome of one validation level.

    :param level: Level name (``structural`` / ``semantic`` / ``operational``
        / ``epistemic``).
    :param passed: True only when the level found nothing wrong.
    :param violations: Human-readable messages; empty when ``passed``.
    """

    level: str
    passed: bool
    violations: List[str] = field(default_factory=list)


def load_shapes_ttl() -> str:
    """Read the packaged SHACL shapes source.

    Uses ``importlib.resources`` rather than a path relative to ``__file__``
    because the agent container and the REST Lambda unpack this package at
    different working directories.

    :returns: The Turtle source of ``rule_shapes.ttl``.
    """
    return (
        importlib.resources.files(__package__)
        .joinpath(_SHAPES_RESOURCE)
        .read_text(encoding="utf-8")
    )


def validate_structural(
    rule_graph: Graph, *, shapes_ttl: Optional[str] = None,
) -> ValidationReport:
    """Level 1 — does the rule conform to the ``vkgr:`` schema?

    Runs pyshacl over ``rule_shapes.ttl``. FAIL-CLOSED by design: any error
    (unparseable shapes, pyshacl crash) returns passed=False. This is the
    OPPOSITE of the query-side enum injector's fail-soft contract, because
    this function is an approval gate — a validator that silently passes on
    error is worse than no validator.

    :param rule_graph: The rule's RDF graph (from ``rule_to_graph``).
    :param shapes_ttl: Override shapes source; defaults to the packaged
        ``rule_shapes.ttl``. Present so tests can inject a broken graph.
    :returns: A ``ValidationReport`` for the ``structural`` level.
    """
    try:
        # Imported here, not at module scope: pyshacl pulls in owlrl and is the
        # heaviest dependency in this package. The query path imports
        # rule_models/rule_graph on every request but never validates, so
        # deferring keeps that import cheap.
        import pyshacl

        shapes = Graph()
        shapes.parse(
            data=shapes_ttl if shapes_ttl is not None else load_shapes_ttl(),
            format="turtle",
        )
        conforms, results_graph, _text = pyshacl.validate(
            rule_graph,
            shacl_graph=shapes,
            inference="none",
            advanced=True,
        )
        if conforms:
            return ValidationReport(level="structural", passed=True)
        return ValidationReport(
            level="structural",
            passed=False,
            violations=_violations_from(results_graph),
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed is the contract
        return ValidationReport(
            level="structural",
            passed=False,
            violations=[f"structural validation could not run: {exc}"],
        )


AskCallable = Callable[..., bool]

RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"


def validate_semantic(
    *,
    scope_class: str,
    bindings: List[str],
    outcome_class: str,
    ask: AskCallable,
    ontology_graph_uri: str,
) -> ValidationReport:
    """Level 2 — are the rule's bindings legal for its scope class?

    For every binding, ASK the ontology graph whether the property's
    ``rdfs:domain`` is the scope class or one of its superclasses (walked with a
    ``rdfs:subClassOf*`` property path, so a rule scoped to a subclass may bind a
    parent's property). Also ASK that the conclusion's outcome class is declared.

    FAIL-CLOSED: an ASK that raises is recorded as a violation, not skipped. A
    validator that passes when Neptune is unreachable would let an unchecked rule
    reach PUBLISHED.

    :param scope_class: IRI of the rule's ``vkgr:subjectClass``.
    :param bindings: Property IRIs used by the rule's conditions.
    :param outcome_class: IRI named by the rule's conclusion.
    :param ask: Injected callable ``(query=..., graph_uri=...) -> bool``.
    :param ontology_graph_uri: The ONTOLOGY graph — never the ``/rules`` graph.
    :returns: A ``ValidationReport`` for the ``semantic`` level.
    """
    violations: List[str] = []
    try:
        for binding in bindings:
            # rdfs:subClassOf* lets a rule on a subclass use an inherited property.
            query = (
                f"ASK {{ <{binding}> <{RDFS}domain> ?d . "
                f"<{scope_class}> <{RDFS}subClassOf>* ?d }}"
            )
            if not ask(query=query, graph_uri=ontology_graph_uri):
                violations.append(
                    f"binding {binding} is not a property of {scope_class} "
                    "(or any of its superclasses)"
                )
        # Accept either declaration form: the VKG builder emits owl:Class, but
        # an imported ontology may only say rdfs:Class.
        outcome_query = (
            f"ASK {{ <{outcome_class}> a ?t . VALUES ?t {{ "
            f"<{OWL}Class> <{RDFS}Class> }} }}"
        )
        if not ask(query=outcome_query, graph_uri=ontology_graph_uri):
            violations.append(
                f"outcome class {outcome_class} is not declared in the ontology"
            )
    except Exception as exc:  # noqa: BLE001 — fail closed, surface the cause
        violations.append(f"semantic validation could not complete: {exc}")
    return ValidationReport(
        level="semantic", passed=not violations, violations=violations
    )


def validate_operational(
    *,
    bindings: List[str],
    select: Callable[..., List[Dict[str, str]]],
    ontology_graph_uri: str,
) -> Tuple[ValidationReport, List[str]]:
    """Level 3 — does every binding resolve to a real physical column?

    Walks ``vkg:mapsToColumn`` (the same predicate the VKG builder writes, see
    ``agents/ontology_agent/enum_shapes.py``) for all bindings in ONE SPARQL
    SELECT, then diffs the returned set against the requested set.

    An unresolved binding is returned as a MISSING INPUT and the report still
    PASSES: per the design, a rule with missing inputs is publishable but not
    evaluable — it answers via advisory and declares the gap. Reporting it as a
    failure instead would make the publish gate reject the rule and render the
    whole advise-rather-than-evaluate path dead code. The violations list still
    names each unmapped binding so a reviewer sees it; it is informational at
    this level, not blocking. Blank column literals count as unmapped (schema
    drift, not a mapping).

    FAIL-CLOSED on a FAILED PROBE only: if the SELECT raises we learned nothing,
    so the report FAILS and every binding is listed missing. Publishing a rule
    whose data mapping could not be verified is the one outcome that must not
    happen. Distinguishing "known unmapped" from "could not check" is the whole
    reason this returns a report AND a list.

    :param bindings: Property IRIs used by the rule's conditions.
    :param select: Injected ``(query=..., graph_uri=...) -> list[dict]`` callable.
    :param ontology_graph_uri: The ontology graph holding the VKG mappings.
    :returns: ``(report, missing_inputs)`` — missing_inputs preserves the input
        order of ``bindings`` so the UI and the persisted rule always agree.
    """
    if not bindings:
        return ValidationReport(level="operational", passed=True, violations=[]), []
    values = " ".join(f"<{b}>" for b in bindings)
    query = (
        f"SELECT ?binding ?column WHERE {{ VALUES ?binding {{ {values} }} "
        f"?binding <{VKG_MAPS_TO_COLUMN}> ?column }}"
    )
    try:
        rows = select(query=query, graph_uri=ontology_graph_uri)
        mapped = {
            str(r["binding"])
            for r in rows
            if str(r.get("column", "")).strip()
        }
    except Exception as exc:  # noqa: BLE001 — fail closed
        return (
            ValidationReport(
                level="operational",
                passed=False,
                violations=[f"column resolution could not complete: {exc}"],
            ),
            list(bindings),
        )
    missing = [b for b in bindings if b not in mapped]
    violations = [
        f"binding {b} has no vkg:mapsToColumn mapping — the rule can advise "
        "but not evaluate until it is mapped"
        for b in missing
    ]
    # passed=True even with missing bindings: see the docstring. The probe
    # succeeded, so we KNOW the mapping state, which is a publishable outcome.
    return (
        ValidationReport(level="operational", passed=True, violations=violations),
        missing,
    )


def validate_epistemic(*, rule: "Rule") -> ValidationReport:
    """Level 4 — does the conclusion claim more than the evidence supports?

    Only ``confirmedCauseOf`` asserts causation, and it is the one status a
    machine may never grant itself: it requires a named human attestor and a
    non-empty justification recorded on the conclusion. All weaker statuses
    (candidate / indicates / supports / associated / requiresFurtherEvidence)
    pass without attestation because they are explicitly non-causal.

    :param rule: The rule being validated.
    :returns: A ``ValidationReport`` for the ``epistemic`` level.
    """
    violations: List[str] = []
    conclusion = rule.conclusion
    if conclusion.epistemic_status == STRONGEST_EPISTEMIC_STATUS:
        if not (conclusion.attested_by or "").strip():
            violations.append(
                f"{STRONGEST_EPISTEMIC_STATUS} requires vkgr:attestedBy — a named "
                "reviewer must take responsibility for a causal claim"
            )
        if not (conclusion.attestation_note or "").strip():
            violations.append(
                f"{STRONGEST_EPISTEMIC_STATUS} requires a non-empty attestation "
                "note citing the evidence for causation"
            )
    return ValidationReport(
        level="epistemic", passed=not violations, violations=violations
    )


def validate_rule(
    *,
    rule: "Rule",
    ask: AskCallable,
    select: Callable[..., List[Dict[str, str]]],
    ontology_graph_uri: str,
) -> Tuple[List[ValidationReport], List[str]]:
    """Run the full four-level validation ladder over one rule.

    Every level runs even if an earlier one fails: a reviewer opening the admin
    UI needs the complete report set in a single pass, not one error at a time.

    :param rule: The rule to validate.
    :param ask: SPARQL ASK callable for level 2.
    :param select: SPARQL SELECT callable for level 3.
    :param ontology_graph_uri: Ontology graph URI (NOT the /rules graph).
    :returns: ``(reports_in_ladder_order, missing_inputs)``.
    """
    from agents.shared.rule_graph import rule_to_graph  # local: avoid a cycle

    bindings = [c.binding for c in rule.conditions]
    structural = validate_structural(rule_to_graph(rule))
    semantic = validate_semantic(
        scope_class=rule.scope.subject_class,
        bindings=bindings,
        outcome_class=rule.conclusion.outcome,
        ask=ask,
        ontology_graph_uri=ontology_graph_uri,
    )
    operational, missing = validate_operational(
        bindings=bindings, select=select, ontology_graph_uri=ontology_graph_uri
    )
    epistemic = validate_epistemic(rule=rule)
    return [structural, semantic, operational, epistemic], missing


def _violations_from(results_graph: Graph) -> List[str]:
    """Render a SHACL results graph as violation strings.

    Each message names the failing property's local name so a reviewer (and the
    ``"conclusion" in violation`` assertions in the tests) can tell which part
    of the rule is wrong without reading RDF.

    :param results_graph: pyshacl's validation report graph.
    :returns: Sorted, de-duplicated violation messages.
    """
    from rdflib.namespace import SH

    messages: List[str] = []
    for result in results_graph.subjects(SH.resultSeverity, None):
        path = results_graph.value(result, SH.resultPath)
        message = results_graph.value(result, SH.resultMessage)
        # Prefer the shape's own sh:message; fall back to the constraint name
        # when a nested shape reports without one.
        detail = str(message) if message is not None else str(
            results_graph.value(result, SH.sourceConstraintComponent) or "violation"
        )
        if path is not None:
            local = str(path).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            messages.append(f"{local}: {detail}")
        else:
            messages.append(detail)
    # Sorted for deterministic output; de-duplicated because a nested shape can
    # report the same defect at both the parent and child node.
    return sorted(set(messages))
