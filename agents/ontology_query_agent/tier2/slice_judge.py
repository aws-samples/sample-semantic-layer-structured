"""Slice-sufficiency judge — wraps a small Strands Agent that returns a
``SliceSufficiency`` structured-output: a bounded structured-output verdict on
whether a retrieved slice covers the question (the analog of an answer-quality
judge, but for slice content rather than the final answer).

Fail-open: a judge timeout/exception returns ``{sufficient: True}`` so the
query path can proceed under a possibly-imperfect slice rather than
hanging on the judge.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# NOTE — this prompt is LAYER-AGNOSTIC by design. It describes the SHAPES of
# schema patterns (association/junction class, bridge class, self-join, label vs
# code) in neutral terms and must NOT hardcode any specific layer's class/column
# names. Layer-specific join/derivation knowledge belongs in the ontology's
# rdfs:comment annotations — which the judge reads in the slice — never here, so
# this judge generalizes to every semantic layer.
_JUDGE_PROMPT = (
    "You decide whether an ontology slice is sufficient to answer a user "
    "question. Output SliceSufficiency JSON only.\n\n"
    "If the slice contains the classes and properties needed to write a "
    "SPARQL query for the question, set sufficient=true and missing=[].\n"
    "If concepts are missing, set sufficient=false and list the IRIs (as "
    "strings, not curies) that you'd want added to the slice.\n"
    "Bias: prefer sufficient=true when the slice contains a clear path "
    "between the candidates the question references; only flag missing when "
    "a critical class or property is absent.\n"
    "The slice text is AUTHORITATIVE and you must read it literally: if a class "
    "or property IRI (or its local name) APPEARS anywhere in the slice, it IS "
    "present — never report a concept as missing when its IRI is in the slice. "
    "Only the absence of a string from the slice text counts as missing. Treat an "
    "rdfs:comment on a class or property as authoritative guidance — when it "
    "states a join key, a lookup, or a derivation to use, honor it as the "
    "connecting path / substitution rather than demanding a separate concept.\n"
    "Concept-level completeness: break the question into the concrete pieces it "
    "asks for (each value to return, filter on, group by, or order by) and check "
    "each maps to a class or property in the slice. A requested value is "
    "SATISFIED as soon as a property whose local name plausibly carries it is "
    "present (e.g. a 'names' request is met by a *Name / *_name / name property on "
    "the relevant class; a 'how many' needs only the class). Set sufficient=false "
    "ONLY when a requested piece has NO plausibly-matching property anywhere in "
    "the slice — not because the name is spelled differently than you expected.\n"
    "Relationship connectivity (also OVERRIDES the path bias): when the question "
    "RELATES two entities (e.g. 'X items grouped BY Y', 'X and their related Z', "
    "'the entity in role A that is also in role B'), verify the slice actually "
    "contains a connecting PATH between those classes — a chain of object "
    "properties (via rdfs:domain / rdfs:range) and/or rdfs:subClassOf edges that "
    "links them. A path counts whether it is a direct object property OR a "
    "multi-hop chain through an intermediate/association/bridge class that joins "
    "to BOTH (a bridge class carrying both entities' keys is equivalent to a "
    "direct foreign key). If the two classes are present but nothing connects "
    "them, set sufficient=false and name the linking class / property you'd add in "
    "missing[] (the builder will expand the slice to pull it in).\n"
    "Derivation & substitution before missing[] (this OVERRIDES the connectivity "
    "check and is the dominant false-negative — apply it before rejecting any "
    "relationship/role question): a question does NOT require a property literally "
    "named after its surface wording. Before flagging a role, relationship, or "
    "value as missing, ask whether it can be DERIVED from properties already in "
    "the slice — by a self-join on an association class, or by substituting an "
    "equivalent property reached over a present path. A need is "
    "DERIVABLE/SATISFIED (so sufficient=true, missing=[]) when:\n"
    "  - A ROLE or category the question asks for is carried by a different but "
    "semantically-equivalent property on a connected class (e.g. a "
    "type/category/status property on a related class, reached over a present "
    "path, supplies it). Do NOT require a literally-named role property or a "
    "dedicated role class when such an equivalent property is present. An EMPTY or "
    "absent association class is NOT a gap when the value is derivable from a "
    "related class that IS present.\n"
    "  - A relationship like 'the same entity appears in two roles on one parent' "
    "is expressible by SELF-JOINING / GROUP BY…HAVING on a single "
    "association/junction class that already holds the needed keys — i.e. one "
    "class carrying BOTH a parent-record key AND a related-entity key (plus a row "
    "discriminator) lets you match its own rows (same parent + related keys across "
    ">1 distinct discriminator). Do NOT require a literally-named role property or "
    "a dedicated role/membership class — if such an association class with those "
    "keys is present, the relationship IS satisfied → sufficient=true.\n"
    "Only after applying this derivation test — if a requested role/relationship "
    "has NEITHER a direct property NOR a self-join/substitution route over the "
    "slice's present classes — set sufficient=false and name the genuinely-absent "
    "linking concept; do NOT pass a slice the SPARQL generator could only satisfy "
    "by inventing a predicate or role value that does not exist in the ontology.\n"
    "Anti-over-rejection (READ LAST, it bounds every check above): the checks "
    "above guard against passing an UNANSWERABLE slice — they are NOT a licence to "
    "reject an answerable one. A simple single-class question (e.g. a count of one "
    "class, or a list of one class by a name property) is SUFFICIENT as soon as "
    "that one class and the property it asks for are present — do not demand a "
    "join, a role, or a lookup it never asked for. Only list an IRI in missing[] "
    "when it is a concept you can NAME a concrete need for AND it is genuinely "
    "absent from the slice; NEVER invent a plausible-sounding class/property IRI "
    "just because it would be convenient — a fabricated IRI can never be fetched, "
    "so it makes the slice loop forever without converging. When in doubt and the "
    "core classes the question names are present, prefer sufficient=true."
)


class SliceSufficiency(BaseModel):
    """Structured-output schema for the slice judge."""

    sufficient: bool = Field(
        description="True iff the slice is enough to write a query.")
    missing: List[str] = Field(
        default_factory=list,
        description="IRIs the agent would want added.")


# Max slice characters handed to the judge. The builder already bounds the slice
# to SLICE_TOKEN_BUDGET (12000) tokens ≈ ~48000 chars; the previous 8000-char cap
# silently discarded ~80% of the budgeted slice, so a candidate class/property
# beyond char 8000 in the TTL was hidden from the judge → it (correctly, given its
# truncated view) reported the concept missing. The judge model (Sonnet) has ample
# context, so align the cap with the builder budget instead of throttling it.
_DEFAULT_SLICE_CHAR_LIMIT = 48000


def build_slice_judge(*, model_factory: Callable[[], Any],
                      agent_factory: Optional[Callable[..., Any]] = None,
                      system_prompt: Optional[str] = None,
                      slice_char_limit: int = _DEFAULT_SLICE_CHAR_LIMIT,
                      ) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Return a callable judge that evaluates ``{"slice", "question"}`` payloads.

    Args:
        model_factory: Zero-arg callable returning a Strands model — invoked
            lazily when ``agent_factory`` is not supplied so tests can pass a
            ``MagicMock`` without a real Bedrock model.
        agent_factory: Optional override that returns a callable agent. The
            production path constructs a Strands ``Agent`` with the
            ``SliceSufficiency`` structured-output schema.
        system_prompt: Optional override for the judge system prompt. Defaults
            to the module-local ``_JUDGE_PROMPT``; metadata_query_agent passes
            its copy from ``agents.metadata_query_agent.query_prompts.JUDGE_PROMPT``.
        slice_char_limit: Max slice characters passed to the judge. Defaults to
            ~the builder's token budget so the judge sees the WHOLE budgeted slice
            (the prior 8000 hid budgeted concepts from the judge → false missing).
    """
    prompt = system_prompt or _JUDGE_PROMPT
    if agent_factory is None:
        from strands import Agent  # local import keeps unit tests light

        def _factory(**kw: Any) -> Any:
            return Agent(
                model=model_factory(),
                system_prompt=prompt,
                tools=[],
                structured_output_model=SliceSufficiency,
            )

        agent_factory = _factory

    def _judge_usage(result: Any) -> Dict[str, int]:
        """Return token usage from a Strands judge result (zeros if absent).

        Only reads usage when ``accumulated_usage`` is a real dict — the judge
        is often driven by a MagicMock in tests, whose attribute access yields
        non-numeric values we must not coerce. Captures cache-read/write tokens
        too (Bedrock folds them into totalTokens under cache_config=auto) so the
        running total reconciles with the in/out breakdown.
        """
        keys = (
            "inputTokens",
            "outputTokens",
            "totalTokens",
            "cacheReadInputTokens",
            "cacheWriteInputTokens",
        )
        out = {k: 0 for k in keys}
        try:
            acc = result.metrics.accumulated_usage
        except AttributeError:
            return out
        if not isinstance(acc, dict):
            return out
        for key in keys:
            value = acc.get(key)
            if isinstance(value, (int, float)):
                out[key] = int(value)
        return out

    def judge(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate slice sufficiency, defaulting to sufficient on failure."""
        try:
            agent = agent_factory()
            result = agent(json.dumps({
                "slice": payload["slice"][:slice_char_limit],
                "question": payload["question"],
            }))
            so = result.structured_output
            return {
                "sufficient": bool(so.sufficient),
                "missing": list(so.missing or []),
                "usage": _judge_usage(result),
            }
        except Exception as e:  # noqa: BLE001 - fail-open is intentional
            logger.warning(
                "slice judge failed (%s) — defaulting to sufficient", e,
            )
            return {"sufficient": True, "missing": [], "usage": {}}

    return judge
