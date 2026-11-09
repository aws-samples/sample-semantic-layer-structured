"""Phase 4 (Hybrid): NL→SQL generation with ontology-derived negative constraints.

Copied-and-owned (per the design's copy-not-import rule) from the Metadata
agent's ``tier2/rag_query_generator.py``, but re-shaped around an INJECTED
``invoke_model`` seam instead of a Strands ``agent_factory``. That seam is a
plain ``(prompt: str, **kwargs) -> str`` callable returning the model's SQL
text, so this module has NO direct Bedrock/AWS dependency and the unit test can
drive it with a lambda.

The Hybrid-specific addition is the ``negative_constraint`` parameter on
:meth:`SqlGenerator.generate`. The grounding gate
(:mod:`agents.semantic_layer_query_agent.grounding`) emits ontology-derived
"missing" tags (e.g. ``property:<iri> on <class_iri>``) naming a predicate/
column that the previously-generated SQL used but that is NOT grounded in the
slice. On the REGENERATE path we thread that tag in as a negative constraint so
the model avoids the flagged identifier on the next attempt.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, Tuple

from .query_prompts import (
    SQL_GENERATION_GUIDANCE,
    SQL_GENERATION_HEADER_TEMPLATE,
)

logger = logging.getLogger(__name__)

# Matches a leading/trailing markdown code fence (```sql … ``` or ``` … ```) so
# we can strip it if the model wraps the SQL despite the "no markdown"
# instruction. Kept defensive: the prompt forbids fences, but models sometimes
# add them anyway and an unstripped fence breaks the sqlglot parse downstream.
_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE)


class SqlGenerator:
    """Generate SQL from a slice + question, with optional negative constraints.

    The model is reached through an injected ``invoke_model`` callable (the
    "seam") so this class never imports Bedrock/Strands directly — construction
    and generation work with a plain lambda in unit tests.
    """

    def __init__(self, *, invoke_model: Callable[..., Any]) -> None:
        """Construct the generator.

        Args:
            invoke_model: A callable ``(prompt: str, **kwargs) -> str`` that
                sends ``prompt`` to the model and returns the model's SQL text.
                It MAY instead return a ``(sql_text, usage_dict)`` tuple or a
                dict carrying ``text``/``sql`` and ``usage`` keys; any usage is
                folded into :attr:`last_usage`. Injected so no Bedrock call
                happens on the constructor path (keeps the unit test AWS-free).
        """
        self.invoke_model = invoke_model
        # Token usage from the most recent generate() call. Defaults to zeros and
        # is overwritten only when the seam reports usage — so a seam that returns
        # a bare SQL string (as the unit test does) simply leaves zeros.
        self.last_usage: Dict[str, int] = {
            "inputTokens": 0,
            "outputTokens": 0,
            "totalTokens": 0,
        }

    def _build_prompt(
        self,
        *,
        slice_text: str,
        question: str,
        negative_constraint: str,
    ) -> str:
        """Assemble the NL→SQL prompt, appending the negative constraint if set.

        Args:
            slice_text: The serialized schema slice (JSON string).
            question: The natural-language question.
            negative_constraint: An ontology-derived tag naming a predicate/
                column the previous SQL misused (e.g.
                ``property:<iri> on <class_iri>``). When non-empty, a
                ``# Do NOT use: <negative_constraint>`` line is appended so the
                model avoids that identifier on regeneration.

        Returns:
            The fully-rendered prompt string.
        """
        suffix = ""
        if negative_constraint:
            # Appended verbatim: the constraint string carries the flagged
            # predicate/column, so appending it whole gives the model the exact
            # identifier to avoid. The "# Do NOT use:" prefix is the negative
            # signal the generation prompt is trained to respect.
            suffix += (
                f"\n\n# Do NOT use: {negative_constraint}\n"
                "The identifier above is NOT grounded in the slice — it was "
                "flagged by the ontology grounding gate on a previous attempt. "
                "Rewrite the query WITHOUT it, using ONLY tables and columns "
                "present in the slice above."
            )
        # Format ONLY the header (the guidance body holds literal JSON braces
        # that must not pass through str.format), then append the guidance
        # verbatim.
        header = SQL_GENERATION_HEADER_TEMPLATE.format(
            slice_text=slice_text,
            question=question,
            suffix=suffix,
        )
        return f"{header}{SQL_GENERATION_GUIDANCE}"

    def _invoke(self, prompt: str) -> str:
        """Call the injected seam and normalize its return into SQL text.

        Folds any usage the seam reports into :attr:`last_usage` and strips a
        markdown fence if the model wrapped the SQL despite instructions.

        Args:
            prompt: The rendered generation prompt.

        Returns:
            The trimmed, fence-stripped SQL string.

        Raises:
            TypeError: If the seam's return value is not a recognized shape
                (str, ``(text, usage)`` tuple, or a dict with a text field) —
                fail loud rather than silently returning garbage.
        """
        result = self.invoke_model(prompt)

        sql_text, usage = self._split_result(result)
        if usage:
            for key, value in usage.items():
                self.last_usage[key] = int(value or 0)
        return self._strip_fence(sql_text)

    @staticmethod
    def _split_result(result: Any) -> Tuple[str, Dict[str, Any]]:
        """Split a seam return value into ``(sql_text, usage)``.

        Args:
            result: Whatever the injected seam returned.

        Returns:
            A ``(sql_text, usage)`` tuple; ``usage`` is empty when the seam
            reported none.

        Raises:
            TypeError: On an unrecognized return shape (fail loud).
        """
        # Bare string (the common case, and what the unit test returns).
        if isinstance(result, str):
            return result, {}
        # (sql_text, usage_dict) tuple.
        if isinstance(result, tuple) and len(result) == 2:
            text, usage = result
            return str(text), dict(usage or {})
        # dict carrying the text under 'sql'/'text' and optional 'usage'.
        if isinstance(result, dict):
            text = result.get("sql", result.get("text"))
            if text is None:
                raise TypeError(
                    "invoke_model dict result missing a 'sql'/'text' key: "
                    f"{sorted(result.keys())}"
                )
            return str(text), dict(result.get("usage") or {})
        raise TypeError(
            "invoke_model must return an SQL str, a (text, usage) tuple, or a "
            f"dict with a 'sql'/'text' key; got {type(result).__name__}"
        )

    @staticmethod
    def _strip_fence(sql: str) -> str:
        """Remove a wrapping markdown code fence and surrounding whitespace.

        Args:
            sql: Raw model output.

        Returns:
            The SQL with any leading ```sql / trailing ``` fence removed.
        """
        stripped = sql.strip()
        if stripped.startswith("```"):
            stripped = _FENCE_RE.sub("", stripped)
        return stripped.strip()

    def generate(
        self,
        *,
        slice_text: str,
        question: str,
        negative_constraint: str = "",
    ) -> str:
        """Generate SQL from the slice + question.

        Args:
            slice_text: The serialized schema slice (JSON string).
            question: The natural-language question.
            negative_constraint: Optional ontology-derived tag (from the
                grounding gate) naming an identifier the previous SQL misused;
                when non-empty it is injected as a ``# Do NOT use: …`` line so
                this regeneration avoids it.

        Returns:
            The generated SQL string (fence-stripped, trimmed).
        """
        # Reset usage for this call so a prior generate() can't bleed through.
        self.last_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
        prompt = self._build_prompt(
            slice_text=slice_text,
            question=question,
            negative_constraint=negative_constraint,
        )
        if negative_constraint:
            logger.info(
                "phase4.sql_regenerate with negative constraint: %s",
                negative_constraint,
            )
        return self._invoke(prompt)
