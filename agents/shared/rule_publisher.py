"""The one server-side gate for the DRAFT/APPROVED -> PUBLISHED transition.

Order matters and is deliberate:

1. lifecycle check  — only an APPROVED rule may publish (a human approved it)
2. four-level validation — cheapest correctness checks first
3. compilation   — refuses unapproved thresholds / missing inputs
4. test-case generation — the five mandatory cases are attached
5. SMOKE EXECUTION — the compiled SPARQL is actually translated and run
   against the LAYER's Athena coordinate

Step 5 is the mitigation for the design's headline risk: Ontop reformulation
gaps. A rule Ontop cannot translate never reaches PUBLISHED, so the failure
surfaces to an admin at authoring time instead of to a user at query time.

The coordinate for step 5 is passed IN (``coordinate``) and is deliberately not
read off the translation. Ontop derives its ``database``/``catalog`` from the
ontology's ``vkg:mapsToTable`` values alone; a HYBRID layer's thin VKG stores
those BARE (``cards``, not ``bird_card_games.cards``), so the derivation names
the alphabetically-first TABLE as the schema and Athena answers
SCHEMA_NOT_FOUND — blocking every rule on every hybrid layer. Only the layer
config knows where the tables actually live, so only the caller can supply it.

Every AWS collaborator is INJECTED (translate, execute_sql, ask, select) so the
whole gate is unit-testable without credentials.
"""
from typing import Any, Callable, Dict, List, Tuple

from agents.shared.rule_compiler import CompileRefused, compile_rule
from agents.shared.rule_models import Rule
from agents.shared.rule_test_cases import generate_test_cases
from agents.shared.rule_validator import validate_rule

SMOKE_LIMIT = 1


class PublishBlocked(Exception):
    """Raised when a rule may not be published. Carries the reason verbatim.

    The message is surfaced to the admin UI, so it must name what failed
    (which level, which threshold, which binding) rather than being generic.
    """


def _smoke_sparql(*, compiled_sparql: str) -> str:
    """Append a LIMIT so the smoke run proves translatability, not throughput.

    :param compiled_sparql: Output of ``compile_rule``.
    :returns: The same query with a ``LIMIT`` clause appended.
    """
    if "LIMIT" in compiled_sparql.upper():
        return compiled_sparql
    return f"{compiled_sparql}\nLIMIT {SMOKE_LIMIT}"


def _require_coordinate(
    *, coordinate: Dict[str, str], rule_id: str
) -> Tuple[str, str]:
    """Extract ``(database, catalog)`` from the layer's Athena coordinate.

    Both halves are mandatory and neither may be defaulted. An absent CATALOG is
    the exact failure the hybrid query path warns about (a federated S3 Tables
    catalog left unnamed resolves against the default Glue catalog and fails
    SCHEMA_NOT_FOUND); an absent DATABASE has nowhere to look at all. Guessing
    either would smoke-test a coordinate the rule will never run against, which
    is what this fix exists to stop.

    :param coordinate: ``{"database": ..., "catalog": ...}``, resolved by the
        caller from the layer's stored configuration.
    :param rule_id: Rule being published — named in the reason, because the
        message is rendered verbatim in the admin UI.
    :returns: ``(database, catalog)``, both non-blank.
    :raises PublishBlocked: If either half is missing or blank. This is an
        admin-actionable state ("build the layer first"), not a server fault.
    """
    database = str(coordinate.get("database") or "").strip()
    catalog = str(coordinate.get("catalog") or "").strip()
    missing = [
        name
        for name, value in (("database", database), ("catalog", catalog))
        if not value
    ]
    if missing:
        raise PublishBlocked(
            f"cannot smoke-execute rule {rule_id}: the layer's configuration "
            f"supplies no Athena {' or '.join(missing)}. Build the layer (or "
            "re-select its data source) before publishing a rule against it."
        )
    return database, catalog


def publish_rule(
    *,
    rule: Rule,
    ask: Callable[..., bool],
    select: Callable[..., List[Dict[str, str]]],
    translate: Callable[..., Dict[str, Any]],
    execute_sql: Callable[..., Dict[str, Any]],
    ontology_graph_uri: str,
    ontology_json: Dict[str, Any],
    coordinate: Dict[str, str],
) -> Rule:
    """Validate, compile and smoke-execute ``rule``, returning a PUBLISHED copy.

    :param rule: The APPROVED rule to publish. NOT mutated.
    :param ask: SPARQL ASK callable (level 2).
    :param select: SPARQL SELECT callable (level 3).
    :param translate: ``(sparql=..., ontology_id=..., ontology_json=...) ->
        {sql, database, catalog}`` — the Ontop translate gateway tool.
    :param execute_sql: ``(sql=..., database=..., catalog=...) -> dict`` — Athena.
    :param ontology_graph_uri: The ontology graph URI (never the /rules graph).
    :param ontology_json: The ontology payload Ontop needs for reformulation.
    :param coordinate: ``{"database", "catalog"}`` for the smoke run, resolved
        by the caller from the LAYER config. Required, and used in preference to
        the translation's own values — see the module docstring for why those
        cannot be trusted. Only consulted on the evaluable branch.
    :returns: A new ``Rule`` with lifecycle PUBLISHED, ``compiled_sparql`` set,
        and the five test cases attached.
    :raises PublishBlocked: On any failed gate. Nothing is persisted.
    """
    if rule.lifecycle != "APPROVED":
        raise PublishBlocked(
            f"rule {rule.rule_id} is {rule.lifecycle}; only an APPROVED rule may "
            "be published — a human must approve it first"
        )

    reports, missing = validate_rule(
        rule=rule, ask=ask, select=select, ontology_graph_uri=ontology_graph_uri
    )
    failed = [r for r in reports if not r.passed]
    if failed:
        detail = "; ".join(
            f"{r.level}: {', '.join(r.violations)}" for r in failed
        )
        raise PublishBlocked(f"validation failed — {detail}")
    test_cases = generate_test_cases(rule=rule)

    # --- The advisory-only branch -------------------------------------------
    # A rule with unresolved inputs (or an unapproved threshold) is a VALID,
    # PUBLISHABLE rule that simply cannot execute. It publishes with
    # compiled_sparql empty and missing_inputs preserved, so rule_lookup
    # retrieves it, is_evaluable() returns False, and the query path routes to
    # advisory with an explicit knowledge_gap.
    #
    # This is the design's "Both — evaluate when fully data-mapped, advise when
    # inputs are missing" decision. Blocking publish here instead would make the
    # advisory branch unreachable and silently discard the mined knowledge that
    # a gap exists — the opposite of "never invent a missing input", which
    # requires NAMING the gap, not dropping the rule.
    unapproved = [
        t.threshold_id for t in rule.thresholds if not t.is_approved()
    ]
    if missing or unapproved:
        return rule.model_copy(
            update={
                "lifecycle": "PUBLISHED",
                "compiled_sparql": "",   # structurally cannot execute
                "missing_inputs": missing,
                "test_cases": test_cases,
            }
        )

    # --- The evaluable branch: compile + prove it actually runs --------------
    try:
        compiled = compile_rule(rule=rule)
    except CompileRefused as exc:
        raise PublishBlocked(f"compilation refused — {exc}") from exc

    # --- Smoke execution: the Ontop-gap mitigation ---------------------------
    # Resolve the coordinate BEFORE translating: an Ontop call we could never
    # execute is wasted, and the resulting error would name Ontop rather than
    # the layer.
    database, catalog = _require_coordinate(
        coordinate=coordinate, rule_id=rule.rule_id
    )
    try:
        translation = translate(
            sparql=_smoke_sparql(compiled_sparql=compiled),
            ontology_id=rule.namespace,
            ontology_json=ontology_json,
        )
    except Exception as exc:  # noqa: BLE001 — reason must reach the admin
        raise PublishBlocked(
            f"Ontop could not translate the compiled rule: {exc}"
        ) from exc
    sql = str(translation.get("sql", "")).strip()
    if not sql:
        raise PublishBlocked("Ontop returned no SQL for the compiled rule")
    try:
        # NOT translation["database"]/["catalog"] — those are derived from the
        # ontology and are wrong for any layer whose mapsToTable is bare.
        execute_sql(sql=sql, database=database, catalog=catalog)
    except Exception as exc:  # noqa: BLE001
        raise PublishBlocked(
            f"smoke execution of the compiled rule failed: {exc}"
        ) from exc

    return rule.model_copy(
        update={
            "lifecycle": "PUBLISHED",
            "compiled_sparql": compiled,
            "missing_inputs": [],
            "test_cases": test_cases,
        }
    )
