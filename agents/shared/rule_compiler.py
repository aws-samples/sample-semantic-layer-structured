"""Compile an approved governed rule into an executable SPARQL SELECT.

Pure and deterministic — there is NO LLM in this path. That is precisely what
makes the compiled artefact reviewable: the same rule always yields the same
query text, so a reviewer who approved a query can trust it will not drift.

Compilation is also the enforcement point for the two governance invariants:
a rule with an unapproved threshold or an unresolved input is REFUSED, never
compiled with a guessed value.
"""
from typing import List

from agents.shared import ontop_envelope
from agents.shared.rule_models import Condition, Rule
from agents.shared.rule_vocab import ALLOWED_OPERATORS

# ALLOWED_OPERATORS is imported, not redefined: the model already refuses any
# operator outside it, so a second copy here could only ever disagree — and a
# disagreement would surface as a publish-time refusal on a rule that
# validated cleanly, which is a confusing way to report a schema mismatch.
# Operators are structural tokens, never interpolated user strings.

SUBJECT_VAR = "?subject"


class CompileRefused(Exception):
    """Raised when a rule may not be compiled.

    Distinct from a validation failure: validation reports on a rule a human is
    still editing, whereas refusal means the caller tried to make a
    non-evaluable rule executable. Fail loudly per project convention.
    """


def _escape_literal(value: str) -> str:
    """Escape a string for a SPARQL double-quoted literal.

    :param value: Raw literal text from the rule.
    :returns: Escaped text safe to place inside ``"..."``.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _var_for(index: int) -> str:
    """Return the SPARQL variable name for condition ``index``.

    Positional naming (``?v0``, ``?v1``) keeps output byte-stable regardless of
    binding IRI text.

    :param index: Zero-based condition position.
    :returns: A SPARQL variable token.
    """
    return f"?v{index}"


def _render_comparand(*, condition: Condition, rule: Rule) -> str:
    """Render the right-hand side of one condition's FILTER.

    :param condition: The condition being compiled.
    :param rule: Owning rule, used to resolve ``threshold_id``.
    :returns: SPARQL term text (a number or a quoted literal).
    :raises CompileRefused: If the threshold is unknown or unapproved.
    """
    if condition.threshold_id is not None:
        threshold = rule.threshold_by_id(threshold_id=condition.threshold_id)
        if threshold is None:
            raise CompileRefused(
                f"condition references unknown threshold {condition.threshold_id}"
            )
        if not threshold.is_approved():
            raise CompileRefused(
                f"threshold {threshold.threshold_id} is not approved; a human must "
                "approve a threshold before it can be executed"
            )
        return repr(threshold.value)
    # Numbers render bare so SPARQL compares numerically; anything else is a
    # quoted, escaped string literal. bool is checked first because in Python
    # bool is a subclass of int, so the numeric branch would catch it.
    if isinstance(condition.value, bool):
        return "true" if condition.value else "false"
    if isinstance(condition.value, (int, float)):
        return repr(condition.value)
    return f'"{_escape_literal(str(condition.value))}"'


def compile_rule(*, rule: Rule) -> str:
    """Compile ``rule`` to a SPARQL SELECT over its scope class.

    :param rule: The rule to compile. Must be evaluable.
    :returns: SPARQL text, already passed through the Ontop envelope rewriters.
    :raises CompileRefused: If the rule has missing inputs, an unapproved or
        unknown threshold, or an operator outside :data:`ALLOWED_OPERATORS`.
    """
    if rule.missing_inputs:
        raise CompileRefused(
            "rule has unresolved inputs and must not be compiled: "
            + ", ".join(rule.missing_inputs)
        )
    patterns: List[str] = [f"  {SUBJECT_VAR} a <{rule.scope.subject_class}> ."]
    filters: List[str] = []
    for index, condition in enumerate(rule.conditions):
        # Defence in depth: Condition already rejects these at construction,
        # but a Rule can also arrive from RDF, so re-check before emitting.
        if condition.operator not in ALLOWED_OPERATORS:
            raise CompileRefused(
                f"operator {condition.operator!r} is not in the allowlist"
            )
        var = _var_for(index)
        patterns.append(f"  {SUBJECT_VAR} <{condition.binding}> {var} .")
        comparand = _render_comparand(condition=condition, rule=rule)
        filters.append(f"  FILTER({var} {condition.operator} {comparand})")
    projection = " ".join(
        [SUBJECT_VAR] + [_var_for(i) for i in range(len(rule.conditions))]
    )
    body = "\n".join(patterns + filters)
    sparql = f"SELECT {projection} WHERE {{\n{body}\n}}"
    # Inherit every Ontop-translatability rewrite the VKG query path relies on,
    # so a compiled rule cannot fail at execution for a reason the VKG path
    # already solved.
    return ontop_envelope.apply(sparql)
