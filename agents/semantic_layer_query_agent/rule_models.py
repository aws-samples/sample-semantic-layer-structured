"""Governed-rule record — the neutral intermediate representation.

Mirrors ``metric_models.py`` in placement and intent, but a Rule is a
CONDITIONAL statement, not an aggregate: it carries scope, conditions,
thresholds, epistemic status, provenance and missing-input register.

Lives under ``agents/shared/`` so both the REST API Lambda and the agent
runtime container import the same model — no cross-deployment imports.

RDF (de)serialization lives in ``rule_graph.py``; this module is pure
validation so it stays importable without a Neptune endpoint.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

from .rule_vocab import (
    ALLOWED_OPERATORS, DEFAULT_EPISTEMIC_STATUS, EPISTEMIC_STATUSES,
    LIFECYCLES, TEST_CASE_KINDS,
)

# Literal types generated from the vocab tuples so the closed sets are
# declared in exactly one place (rule_vocab) and enforced by pydantic here.
EpistemicStatus = Literal[EPISTEMIC_STATUSES]  # type: ignore[valid-type]
Lifecycle = Literal[LIFECYCLES]  # type: ignore[valid-type]
TestCaseKind = Literal[TEST_CASE_KINDS]  # type: ignore[valid-type]

# NOT redeclared here — imported from rule_vocab above so the model and the
# compiler validate against the exact same set. See rule_vocab's docstring.

EMBEDDING_TEXT_SEPARATOR = " | "
"""Separator for KNN match text. Shared by the model and by hydration; changing
it invalidates every already-indexed rule, so it is a named constant."""


def embedding_text_for(
    *, name: str, description: str, synonyms: List[str],
) -> str:
    """Build the KNN match text for a rule from its matchable fields.

    Same fields a steward would search by (mirrors ``MetricService._embed_text``).

    :param name: Rule name.
    :param description: Rule description.
    :param synonyms: Alternate phrasings.
    :returns: Non-empty parts joined by :data:`EMBEDDING_TEXT_SEPARATOR`;
        empty string when every part is empty.
    """
    parts = [name, description] + list(synonyms or [])
    return EMBEDDING_TEXT_SEPARATOR.join(p for p in parts if p)


class Provenance(BaseModel):
    """Where a rule came from. Travels with the rule for its whole life
    (article principle 7) — a rule without this cannot be reviewed."""

    document_id: str
    page: Optional[int] = None
    source_sentence: str = Field(min_length=1)
    model_id: str
    confidence: float = Field(ge=0.0, le=1.0)


class Threshold(BaseModel):
    """A numeric bound as a FIRST-CLASS node, not an inline number.

    ``source_span`` is mandatory and non-empty: this is the structural
    enforcement of "never invent a missing threshold" (design §5). A bound
    the document never stated cannot be represented here at all — it must
    be recorded as a ``missing_inputs`` entry instead.

    ``approved_by`` is the single human gate the article insists on; the
    compiler refuses any rule whose thresholds are unapproved.
    """

    threshold_id: str
    value: float
    unit: str
    source_span: str = Field(min_length=1)
    approved_by: Optional[str] = None

    def is_approved(self) -> bool:
        """True when a named human has approved this bound."""
        return bool(self.approved_by and self.approved_by.strip())


class Condition(BaseModel):
    """One clause. Compares an ontology-property binding against either an
    approved threshold OR a literal value — exactly one, never both."""

    binding: str = Field(min_length=1)
    operator: str
    threshold_id: Optional[str] = None
    value: Optional[Union[str, float, bool]] = None

    @model_validator(mode="after")
    def _exactly_one_comparand(self) -> "Condition":
        """Fail loudly: neither comparand means the clause is unevaluable;
        both means the compiled SPARQL would be ambiguous."""
        if self.operator not in ALLOWED_OPERATORS:
            raise ValueError(
                f"operator must be one of {sorted(ALLOWED_OPERATORS)}"
            )
        has_threshold = self.threshold_id is not None
        has_value = self.value is not None
        if has_threshold == has_value:
            raise ValueError(
                "condition needs exactly one of threshold_id or value"
            )
        return self


class Conclusion(BaseModel):
    """What the rule concludes, and how strongly. ``epistemic_status`` is
    constrained to the closed set so a correlation cannot be stored as a
    confirmed cause (article principle 6).

    The attestation lives here rather than on the ``Rule`` because it attests to
    THIS claim at THIS strength: ``confirmedCauseOf`` is the one status a
    machine may never grant itself, and ``validate_epistemic`` refuses it unless
    both fields below are filled in by a named human.
    """

    outcome: str = Field(min_length=1)
    epistemic_status: EpistemicStatus = DEFAULT_EPISTEMIC_STATUS
    attested_by: Optional[str] = None
    attestation_note: Optional[str] = None


class RuleScope(BaseModel):
    """Binds the rule to an operating context (article Stage 4)."""

    subject_class: str = Field(min_length=1)
    process_stage: Optional[str] = None


class TestCase(BaseModel):
    """One of the five mandatory cases; all five asserted at publish.

    Carries the actual ``inputs`` rather than only a prose description, so a
    reviewer sees the concrete values a case exercises and the case stays
    machine-checkable. ``subject_class`` is per-case because the
    out-of-scope case deliberately uses a different class from the rule's own
    scope. Generated deterministically by ``rule_test_cases.py``.
    """

    # Tells pytest this is a domain model, not a test class to collect. Without
    # it every test module importing TestCase emits a PytestCollectionWarning.
    __test__ = False

    kind: TestCaseKind
    subject_class: str = Field(min_length=1)
    inputs: Dict[str, Any] = Field(default_factory=dict)
    expected_fires: bool
    rationale: str = Field(min_length=1)


class Rule(BaseModel):
    """The governed intermediate rule. Authored/mined as DRAFT with no
    ``compiled_sparql``; the compiler writes that only at PUBLISHED."""

    rule_id: str
    namespace: str
    name: str
    description: str
    rule_type: Literal["diagnostic", "risk", "compliance", "advisory"]
    level: Literal["general", "contextualised", "decision"]
    scope: RuleScope
    conditions: List[Condition] = Field(min_length=1)
    conclusion: Conclusion
    thresholds: List[Threshold] = Field(default_factory=list)
    provenance: Optional[Provenance] = None
    synonyms: List[str] = Field(default_factory=list)
    missing_inputs: List[str] = Field(default_factory=list)
    test_cases: List[TestCase] = Field(default_factory=list)
    specialises: Optional[str] = None
    lifecycle: Lifecycle = "DRAFT"
    reviewed_by: Optional[str] = None
    compiled_sparql: Optional[str] = None
    version: int = 1

    def threshold_by_id(self, *, threshold_id: str) -> Optional[Threshold]:
        """Look up a referenced threshold, or None when dangling.

        :param threshold_id: Identifier referenced by a condition.
        :returns: The matching threshold, or None.
        """
        for t in self.thresholds:
            if t.threshold_id == threshold_id:
                return t
        return None

    def is_evaluable(self) -> bool:
        """True when the rule may EXECUTE rather than advise.

        Requires: no missing inputs, every referenced threshold present and
        human-approved, and compiled SPARQL on the record. This is the
        single predicate the query path branches on (design §7).

        :returns: True when the rule can be executed as-is.
        """
        if self.missing_inputs:
            return False
        if not self.compiled_sparql:
            return False
        for c in self.conditions:
            if c.threshold_id is None:
                continue
            t = self.threshold_by_id(threshold_id=c.threshold_id)
            if t is None or not t.is_approved():
                return False
        return True

    def embedding_text(self) -> str:
        """Text the KNN index matches questions against.

        :returns: The joined match text for this rule.
        """
        return embedding_text_for(
            name=self.name,
            description=self.description,
            synonyms=self.synonyms,
        )
