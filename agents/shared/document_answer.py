"""Prose-answer terminal — answer FROM uploaded documents, never with SQL.

The third terminal of the query path, alongside SQL execution and the advisory
tier. Where advisory answers questions *about* a layer from its schema metadata,
this answers questions *from* the layer's unstructured documents (an underwriting
guide, a policy wording, a glossary PDF), citing the source file and page.

This is what lets a **document-only** layer be queryable at all: a layer with no
tables produces no SQL candidates, so the document terminal is the only one that
can fire, and Semantic RAG runs purely on unstructured data.

**No-SQL guarantee is structural, not a prompt promise.** Like ``advisory``, this
module is handed a ``synthesize`` callable and a list of passages — it has no
Athena client and no way to execute SQL.

**Grounding is enforced on the way out, not just asked for in the prompt.** Every
citation the model returns is checked against the citations of the passages it was
actually given; unmatched ones are dropped. A model that answers with no valid
citation is treated as insufficient. Prompt instructions are not a guarantee.

Callers wire this in at the point where they would otherwise abstain — never
ahead of the SQL attempt — so a layer with tables behaves exactly as before
whenever SQL succeeds. See ``agents/shared/document_retrieval.py`` for the
retrieval half.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .document_retrieval import DEFAULT_TOP_K, retrieve_document_chunks

logger = logging.getLogger(__name__)

# Synthesis callable: takes a fully-formed prompt, returns the model's text.
# Injected so this module stays free of any Strands/Bedrock import and is
# unit-testable with a stub. Same seam as ``advisory.SynthesizeFn``.
SynthesizeFn = Callable[[str], str]

# Provenance tier for a document answer, alongside the existing 'advisory' tier.
DOCUMENT_TIER: str = 'document'

# Cap on passage text fed to the model. Eight ~800-char KB child chunks fit
# comfortably; this only guards against a pathologically large single chunk.
MAX_PASSAGE_CHARS: int = 6000


@dataclass
class DocumentAnswer:
    """The outcome of a document-grounded answer attempt.

    Attributes:
        answer: The prose answer, or an honest explanation when insufficient.
        citations: Source labels actually used, e.g.
            ``['underwriting-guide.pdf — page 12']``. Verified against the
            supplied passages, never taken from the model on trust.
        sources: Provenance ids in the codebase's convention,
            e.g. ``['doc:<docId>#1']``, parallel to ``rule:<id>``.
        sufficient: False when the passages do not answer the question. The
            caller must then abstain rather than present ``answer`` as fact.
        knowledge_gap: What was missing, when ``sufficient`` is False.
    """

    answer: str
    citations: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    sufficient: bool = False
    knowledge_gap: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        """Serialise into the query agents' standard response shape.

        ``executed_sql`` and ``results`` are always empty — the structural
        no-SQL guarantee, visible in the payload rather than only in the prompt.

        Returns:
            The response dict the query agents return for a document answer.
        """
        return {
            'answer': self.answer,
            'tier': DOCUMENT_TIER,
            'citations': self.citations,
            'sources': self.sources,
            'executed_sql': '',
            'results': [],
            'sufficient': self.sufficient,
            'knowledge_gap': self.knowledge_gap,
        }


DOCUMENT_ANSWER_PROMPT = """\
You answer questions using ONLY the document passages below. These come from \
documents a user uploaded to this semantic layer.

Rules:
1. Use ONLY information in the passages. Do not use outside knowledge, and do \
not infer facts the passages do not state.
2. Cite the source of every claim using the exact bracketed label shown above \
each passage, e.g. [underwriting-guide.pdf - page 12].
3. If the passages do not contain enough information to answer, say so. Do not \
guess, and do not answer a nearby question instead.
4. Do not write SQL. You have no database access.

Question:
{question}

Passages:
{passages}

Respond with ONLY a JSON object, no prose around it:
{{"sufficient": true|false,
  "answer": "<the answer, with [label] citations inline>",
  "citations": ["<exact label of each passage you used>"],
  "knowledge_gap": "<when sufficient is false: what information is missing>"}}
"""


def _format_passages(*, chunks: Sequence[Any]) -> str:
    """Render retrieved passages for the prompt, labelled by their citation.

    Args:
        chunks: ``DocumentChunk``-shaped objects (``citation``, ``text``).

    Returns:
        The passage block, each entry prefixed by its bracketed citation label.
    """
    parts: List[str] = []
    for chunk in chunks:
        text = (chunk.text or '').strip()[:MAX_PASSAGE_CHARS]
        # Strip the chunk's own '## [label]' headings: the label is already the
        # passage's prefix here, and leaving both in invites the model to cite a
        # heading from the middle of a passage as if it were the whole source.
        text = re.sub(r'^##\s*\[[^\]]*\]\s*$', '', text, flags=re.MULTILINE).strip()
        parts.append(f"[{chunk.citation}]\n{text}")
    return '\n\n---\n\n'.join(parts)


def _parse_model_json(*, raw: str) -> Optional[Dict[str, Any]]:
    """Parse the model's JSON response, tolerating a code fence or stray prose.

    Args:
        raw: The model's raw text.

    Returns:
        The parsed dict, or None when no JSON object could be recovered.
    """
    text = (raw or '').strip()
    if not text:
        return None
    # Prefer a fenced block, then fall back to the outermost braces.
    fenced = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not candidate.lstrip().startswith('{'):
        braced = re.search(r'\{.*\}', candidate, re.DOTALL)
        if not braced:
            return None
        candidate = braced.group(0)
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def answer_from_documents(
    *,
    question: str,
    chunks: Sequence[Any],
    synthesize: SynthesizeFn,
) -> DocumentAnswer:
    """Answer a question from retrieved document passages, or abstain.

    Args:
        question: The user's natural-language question.
        chunks: Passages from ``document_retrieval.retrieve_document_chunks``.
        synthesize: Callable ``(prompt) -> str`` that runs the model. Injected
            so this module imports no Strands/Bedrock code.

    Returns:
        A :class:`DocumentAnswer`. ``sufficient`` is False — with a
        ``knowledge_gap`` and no invented answer — whenever there are no
        passages, the model says the passages are insufficient, the response
        cannot be parsed, or the model cites nothing that was actually supplied.
    """
    if not chunks:
        # No model call: there is nothing to ground an answer in, and asking the
        # model anyway is how an unsupported answer gets invented.
        return DocumentAnswer(
            answer=(
                "I can't answer that from this semantic layer's documents — no "
                "uploaded document contains a relevant passage."
            ),
            sufficient=False,
            knowledge_gap='no document passage matched the question',
        )

    prompt = DOCUMENT_ANSWER_PROMPT.format(
        question=question, passages=_format_passages(chunks=chunks)
    )
    try:
        raw = synthesize(prompt)
    except Exception as exc:  # noqa: BLE001 — abstain rather than error the turn
        logger.exception('Document answer synthesis failed')
        return DocumentAnswer(
            answer="I couldn't read this layer's documents to answer that.",
            sufficient=False,
            knowledge_gap=f'document answer synthesis failed: {exc}',
        )

    parsed = _parse_model_json(raw=raw)
    if parsed is None:
        logger.warning('Document answer was not parseable JSON; abstaining')
        return DocumentAnswer(
            answer="I couldn't produce a grounded answer from this layer's documents.",
            sufficient=False,
            knowledge_gap='the document answer could not be parsed',
        )

    answer_text = str(parsed.get('answer') or '').strip()
    gap = str(parsed.get('knowledge_gap') or '').strip() or None

    if not parsed.get('sufficient') or not answer_text:
        return DocumentAnswer(
            answer=answer_text
            or (
                "The uploaded documents don't contain enough information to "
                "answer that."
            ),
            sufficient=False,
            knowledge_gap=gap or 'the documents do not answer the question',
        )

    # Verify citations against what was actually supplied. A citation the model
    # invented would read as provenance while pointing at nothing.
    supplied = {chunk.citation: chunk for chunk in chunks}
    claimed = parsed.get('citations') or []
    citations = [str(c) for c in claimed if str(c) in supplied]
    if not citations:
        logger.warning(
            'Document answer cited %d label(s), none of which were supplied; '
            'abstaining rather than presenting an ungrounded answer',
            len(claimed),
        )
        return DocumentAnswer(
            answer=(
                "I found related passages in this layer's documents but couldn't "
                "tie an answer to a specific source, so I'd rather not guess."
            ),
            sufficient=False,
            knowledge_gap='the answer could not be grounded in a cited passage',
        )

    # Provenance ids follow the existing `rule:<id>` convention. The ordinal is
    # the passage's position in the supplied list, so a source is traceable back
    # to the exact retrieved passage and not merely to the document.
    order = {chunk.citation: i for i, chunk in enumerate(chunks)}
    sources = [
        f"doc:{supplied[c].document_id}#{order[c]}" for c in citations
    ]
    return DocumentAnswer(
        answer=answer_text,
        citations=citations,
        sources=sources,
        sufficient=True,
        knowledge_gap=None,
    )


def try_document_answer(
    *,
    question: str,
    kb_id: str,
    layer_id: str,
    synthesize: SynthesizeFn,
    kb_client: Any,
    top_k: int = DEFAULT_TOP_K,
) -> Optional[DocumentAnswer]:
    """Retrieve-then-answer in one call: the single seam every agent wires in.

    This exists so the three query agents share one *call shape*, not merely one
    implementation. Each agent's insertion point becomes a single guard, and the
    retrieval filter, the grounding check and the abstain policy cannot drift
    apart between VKG, Semantic RAG and Hybrid.

    Returns None — never a partial answer — unless a grounded, sufficient answer
    was produced, so the caller's existing abstain message stays the fallback:

        doc = try_document_answer(...)
        if doc is not None:
            return doc.as_dict()
        # ... existing abstain, unchanged

    Args:
        question: The user's natural-language question.
        kb_id: The semantic-rag Knowledge Base id. An empty value means this
            deployment has no KB (``enableSemanticRag=false``) and yields None.
        layer_id: The semantic layer id, scoping retrieval to its documents.
        synthesize: Callable ``(prompt) -> str`` running the agent's own model.
        kb_client: The agent's ``bedrock-agent-runtime`` client.
        top_k: Maximum passages to retrieve.

    Returns:
        A sufficient, citation-grounded :class:`DocumentAnswer`, or None when the
        layer has no relevant document passage or no grounded answer came back.
    """
    if not kb_id or not layer_id:
        # Not an error: a deployment without the KB, or a request without a
        # resolved layer, simply has no documents to fall back to.
        logger.info(
            'Skipping document terminal (kb_id=%r layer_id=%r)', kb_id, layer_id
        )
        return None

    chunks = retrieve_document_chunks(
        kb_id=kb_id,
        layer_id=layer_id,
        question=question,
        client=kb_client,
        top_k=top_k,
    )
    if not chunks:
        return None

    answer = answer_from_documents(
        question=question, chunks=chunks, synthesize=synthesize
    )
    if not answer.sufficient:
        logger.info(
            'Document terminal abstained for layer %s: %s',
            layer_id,
            answer.knowledge_gap,
        )
        return None
    return answer
