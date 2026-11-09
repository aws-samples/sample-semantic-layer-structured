"""Execute a PUBLISHED governed rule and render an epistemically honest answer.

Two hops, both injected so this module needs neither an MCP client nor boto3:
SPARQL → SQL via the Ontop gateway tool, then SQL on Athena. This is the same
path Tier 2's Phase 5 uses (``ontology_query_agent/main.py`` ``_run_execution``),
minus the LLM repair loop — a rule's SPARQL is compiled deterministically and
smoke-executed at publish, so a failure here is a regression to surface, not
something to repair. Every failure raises ``RuleExecutionError`` and the caller
falls through to Tier 2.

The Athena coordinate for the second hop comes from the CALLER, not from the
translation: Ontop derives the database from ``vkg:mapsToTable``, which is dotted
on a VKG layer and deliberately bare on a hybrid one, so only the caller knows
which source is right for its layer type (see :func:`_resolve_coordinate`).

The answer wording is deliberately template-driven rather than model-generated:
the strength of a causal claim is exactly what must not drift, so the epistemic
phrase comes from a fixed table keyed by ``rule_vocab.EPISTEMIC_STATUSES``.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class RuleExecutionError(RuntimeError):
    """Raised when a rule cannot be executed. Caller falls through to Tier 2."""


# One plain-language phrase per epistemic status. Keys must exactly match
# ``rule_vocab.EPISTEMIC_STATUSES`` — a missing key would render a bare IRI to a
# user, and a test pins the two sets equal.
EPISTEMIC_PHRASE: Dict[str, str] = {
    "candidateContributorTo": (
        "a candidate contributing factor — not a confirmed cause"
    ),
    "indicatesRiskOf": "an indicator of risk",
    "supportsHypothesis": "supporting evidence for the hypothesis",
    "associatedWith": "an association, with no direction of causation implied",
    "requiresFurtherEvidence": "a signal that requires further evidence",
    "confirmedCauseOf": "a confirmed cause, attested by a reviewer",
}


def _applied_thresholds(*, rule: Any) -> List[Any]:
    """Return the thresholds the rule actually applies.

    Scoped to the thresholds a CONDITION references, which is exactly what
    ``Rule.is_evaluable`` and ``rule_compiler._render_comparand`` look at. This
    matters twice over:

      * A gate stricter than the predicate the caller branched on would refuse a
        rule the caller was told to execute, and the fall-through to Tier 2 would
        be indistinguishable from an Ontop failure.
      * An orphan threshold was never used, so citing it in the answer would
        claim a bound the rule did not apply — and an unapproved orphan can reach
        a PUBLISHED rule, so it would read as "approved by None".

    A rule exposing no conditions has nothing to scope by; the real ``Rule``
    requires at least one, so this only arises for a caller passing a partial
    object, and returning every threshold keeps such a caller on the strict side.

    :param rule: The rule whose thresholds to scope.
    :returns: The thresholds referenced by at least one condition.
    """
    conditions = getattr(rule, "conditions", None) or []
    referenced = {
        getattr(c, "threshold_id", None) for c in conditions
        if getattr(c, "threshold_id", None)
    }
    thresholds = getattr(rule, "thresholds", None) or []
    if not referenced:
        return list(thresholds)
    return [t for t in thresholds if t.threshold_id in referenced]


def unapproved_threshold_ids(*, rule: Any) -> List[str]:
    """Return the ids of applied thresholds no human has approved.

    Public because the query-path caller needs the SAME predicate this module
    refuses on: it decides execute-vs-advise, and it also names the unapproved
    bounds in the advisory knowledge gap. Two independent copies of the rule
    would fail invisibly in both directions — a stricter caller advises forever,
    a laxer one is refused here and falls through to Tier 2 forever.

    :param rule: The rule to inspect.
    :returns: Threshold ids that block execution; empty when none do.
    """
    return [
        t.threshold_id for t in _applied_thresholds(rule=rule) if not t.is_approved()
    ]


def _refuse_unless_evaluable(*, rule: Any) -> None:
    """Raise unless the rule is safe to execute.

    Re-checks the evaluate/advise switch at the execution boundary. The caller
    checks it too; duplicating the check here means no future caller can execute
    a rule with an unresolved input, an unapproved threshold or an unfinished
    lifecycle by accident.

    :param rule: The candidate rule.
    :raises RuleExecutionError: If the rule is not PUBLISHED, inputs are missing,
        an applied threshold is unapproved, or no compiled SPARQL exists.
    """
    lifecycle = getattr(rule, "lifecycle", "")
    if lifecycle != "PUBLISHED":
        raise RuleExecutionError(
            f"{rule.rule_id} is {lifecycle or 'unset'}, not PUBLISHED"
        )
    if getattr(rule, "missing_inputs", None):
        raise RuleExecutionError(
            f"{rule.rule_id} has unresolved inputs: {rule.missing_inputs}"
        )
    unapproved = unapproved_threshold_ids(rule=rule)
    if unapproved:
        raise RuleExecutionError(
            f"{rule.rule_id} has unapproved thresholds: {unapproved}"
        )
    if not (getattr(rule, "compiled_sparql", "") or "").strip():
        raise RuleExecutionError(f"{rule.rule_id} has no compiled SPARQL")


def _resolve_coordinate(
    *,
    rule_id: str,
    coordinate: Optional[Tuple[str, str]],
    translated: Dict[str, Any],
) -> Tuple[str, str]:
    """Decide which ``(catalog_id, database_name)`` the rule's SQL runs against.

    Ontop derives the database from ``vkg:mapsToTable`` by taking the part before
    the dot, which is the schema for a VKG layer's dotted name but the
    alphabetically-first TABLE for a hybrid layer's deliberately bare one. So the
    right source depends on the layer type, which only the caller knows.

    :param rule_id: Rule id, for the error message.
    :param coordinate: The caller's pair, or None to defer to the translation
        (correct only where ``mapsToTable`` is dotted, i.e. a VKG layer).
    :param translated: The gateway's translation payload.
    :returns: ``(catalog_id, database_name)`` to execute with.
    :raises RuleExecutionError: If a pair is supplied with either half empty — a
        malformed layer config, not a request to defer.
    """
    if coordinate is None:
        return translated.get("catalog", ""), translated.get("database", "")
    catalog_id, database_name = coordinate
    # An empty half means the caller HAS a config and it lacks the coordinate
    # (``_catalog_database_for_layer`` returns ("", "") for a config with no
    # dataSources). Falling back to the translation here would silently restore
    # the very SCHEMA_NOT_FOUND this parameter exists to prevent.
    if not catalog_id or not database_name:
        raise RuleExecutionError(
            f"{rule_id} was given an incomplete coordinate "
            f"(catalog={catalog_id!r}, database={database_name!r}); "
            "pass None to defer to the translation instead"
        )
    return catalog_id, database_name


def execute_rule(
    *,
    rule: Any,
    ontology_json: Dict[str, Any],
    ontology_id: str,
    translate: Callable[..., Dict[str, Any]],
    execute_sql: Callable[..., Dict[str, Any]],
    coordinate: Optional[Tuple[str, str]],
) -> Dict[str, Any]:
    """Translate the rule's compiled SPARQL and run it.

    :param rule: A PUBLISHED ``Rule``.
    :param ontology_json: The ``get_ontology_from_neptune`` payload, carrying the
        OBDA mappings Ontop needs.
    :param ontology_id: Ontology id, passed through so the Ontop Handler keys its
        warm reformulator cache on a stable id.
    :param translate: ``(sparql, ontology_json, ontology_id) -> {sql, database,
        catalog}`` — normally ``NeptuneGatewayClient.translate_sql``.
    :param execute_sql: ``(sql, database_name, catalog_id) -> {columns, rows,
        state_change_reason}`` — normally ``_run_athena_sql``.
    :param coordinate: ``(catalog_id, database_name)`` the layer's tables live in,
        or None to use the translation's own derivation. REQUIRED (though it may
        be None) because the correct source differs by layer type: see
        :func:`_resolve_coordinate`. A hybrid caller passes its layer config's
        pair; a VKG caller passes None.
    :returns: ``{columns, rows, sql, database, catalog}``, where database/catalog
        are the pair actually executed against — not necessarily the translation's.
    :raises RuleExecutionError: On refusal, an incomplete coordinate, translation
        failure, or query failure.
    """
    _refuse_unless_evaluable(rule=rule)

    try:
        translated = translate(
            sparql=rule.compiled_sparql,
            ontology_json=ontology_json,
            ontology_id=ontology_id,
        )
    except Exception as exc:  # noqa: BLE001 — normalise to one error type
        raise RuleExecutionError(f"{rule.rule_id} translation raised: {exc}") from exc
    # translate_sql reports a failed reformulation as {"error": ...} rather than
    # raising, so both the error key and a missing sql must be inspected.
    sql = (translated or {}).get("sql") or ""
    if not sql or translated.get("error"):
        raise RuleExecutionError(
            f"{rule.rule_id} translation failed: "
            f"{translated.get('error') or 'no sql returned'}"
        )

    catalog_id, database_name = _resolve_coordinate(
        rule_id=rule.rule_id, coordinate=coordinate, translated=translated,
    )

    try:
        result = execute_sql(
            sql=sql,
            database_name=database_name,
            catalog_id=catalog_id,
        )
    except Exception as exc:  # noqa: BLE001 — a boto3 ClientError propagates out
        raise RuleExecutionError(f"{rule.rule_id} execution raised: {exc}") from exc
    # _run_athena_sql reports QUERY failures as a dict with state_change_reason
    # set rather than raising, so it must be inspected, not assumed absent.
    if result.get("state_change_reason"):
        raise RuleExecutionError(
            f"{rule.rule_id} query failed: {result['state_change_reason']}"
        )

    logger.info(
        "execute_rule: %s returned %d row(s) from %s.%s",
        rule.rule_id, len(result.get("rows", [])), catalog_id, database_name,
    )
    # The coordinate reported back is the one that RAN, not the translation's —
    # this dict feeds the response metadata and the log line above, so echoing an
    # unused pair would point the next reader of this bug at the wrong place.
    return {
        "columns": result.get("columns", []),
        "rows": result.get("rows", []),
        "sql": sql,
        "database": database_name,
        "catalog": catalog_id,
    }


def build_rule_answer(*, rule: Any, result: Dict[str, Any]) -> str:
    """Render the answer sentence for an executed rule.

    States the row count, the epistemic status in words, each APPLIED threshold
    with its approver, and the source document — so the user sees the strength of
    the claim and who stands behind the numbers, not just a count.

    :param rule: The executed rule.
    :param result: The ``execute_rule`` result (or an Athena result dict).
    :returns: The answer text.
    """
    rows: List[Any] = result.get("rows", [])
    # Local name of the outcome IRI, whether it ends in a fragment or a segment.
    outcome = rule.conclusion.outcome.rstrip("/#").split("/")[-1].split("#")[-1]
    phrase = EPISTEMIC_PHRASE.get(
        rule.conclusion.epistemic_status, rule.conclusion.epistemic_status,
    )
    if rows:
        head = (
            f"{len(rows)} record(s) meet the conditions for {outcome}, "
            f'which the rule "{rule.name}" treats as {phrase}.'
        )
    else:
        head = (
            f"No records meet the conditions for {outcome} under the rule "
            f'"{rule.name}".'
        )

    parts = [head]
    thresholds = [
        f"{t.threshold_id} = {t.value:g}"
        + (f" {t.unit}" if t.unit else "")
        + f" (approved by {t.approved_by})"
        for t in _applied_thresholds(rule=rule)
    ]
    if thresholds:
        parts.append("Thresholds applied: " + "; ".join(thresholds) + ".")

    provenance = getattr(rule, "provenance", None)
    if provenance is not None and getattr(provenance, "document_id", ""):
        citation = f"Source: {provenance.document_id}"
        # page is Optional[int] on the model, so 0 and None both mean "no page".
        if getattr(provenance, "page", None):
            citation += f", page {provenance.page}"
        parts.append(citation + ".")
    return " ".join(parts)
