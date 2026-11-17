"""IRI constants for the ``vkgr:`` governed-rule vocabulary.

Kept in its own module (no rdflib-heavy imports beyond Namespace) so the
doc-pipeline ``ruleminer`` Lambda — which bundles standalone — can vendor
the same constants without pulling the whole agents/shared tree.
"""
from __future__ import annotations

from rdflib.namespace import Namespace

VKGR = Namespace("https://semantic-layer.aws/virtual-kg/rules#")
"""Rule vocabulary namespace. Changing this orphans every stored rule."""

VKG_MAPS_TO_COLUMN = "https://semantic-layer.aws/virtual-kg/mapsToColumn"
"""Predicate tying a datatype property to its source ``table.column``.
Must stay identical to ``agents/ontology_agent/enum_shapes.py`` — the
operational validator walks it to prove a binding resolves to real data."""

EPISTEMIC_STATUSES: tuple[str, ...] = (
    "associatedWith",
    "requiresFurtherEvidence",
    "candidateContributorTo",
    "supportsHypothesis",
    "indicatesRiskOf",
    "confirmedCauseOf",
)
"""Closed set, ordered WEAKEST → STRONGEST. The extraction parser clamps
DOWN this ordering and never up (design §5), so index order is a contract."""

DEFAULT_EPISTEMIC_STATUS = "candidateContributorTo"
"""What a hedged or unclear source statement collapses to."""

STRONGEST_EPISTEMIC_STATUS = "confirmedCauseOf"
"""Requires explicit reviewer attestation — see the epistemic validator."""

RULES_GRAPH_SUFFIX = "/rules"
"""Appended to the ontology graph URI to form the sibling rules graph."""

LIFECYCLES: tuple[str, ...] = ("DRAFT", "APPROVED", "PUBLISHED")
"""Mirrors ``MetricLifecycle`` so the two artefacts read the same way."""

TEST_CASE_KINDS: tuple[str, ...] = (
    "positive", "negative", "boundary", "missing_data", "out_of_scope",
)
"""The article's five mandatory test cases; all five asserted at publish."""

ALLOWED_OPERATORS: frozenset[str] = frozenset(
    {"=", "!=", ">", ">=", "<", "<="}
)
"""Scalar comparison operators a condition may use — the SINGLE declaration.

``rule_models.Condition`` validates against this set and ``rule_compiler``
renders from it. Declaring it twice would allow a rule to pass model
validation and then be refused at publish, which reads as an Ontop bug
rather than the schema mismatch it actually is.

Deliberately excludes set membership (``in``): the compiler emits one
``FILTER(?v <op> <comparand>)`` per condition against a single scalar
comparand, and ``Condition`` holds one ``value``, not a list. Adding ``in``
means extending the model, the compiler, and the boundary-test generator
together — out of scope for v1.
"""
