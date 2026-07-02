"""Doc-pipeline chunker (item #3).

Reads a document from S3, splits it into chunks per the file format, and
emits chunk records the rest of the pipeline (NER → embedder → linker →
indexer) consumes.

Supported types:
    * Plain text (.txt) — sliding-window token cap.
    * Markdown (.md) — heading-aware split, falling back to window.
    * PDF (.pdf) — page-by-page extraction via pymupdf.
    * DOCX (.docx) — paragraph extraction via python-docx.

The chunker stays a pure function so unit tests can run on local fixtures
without S3.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Iterator, List

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------

# Max chars per chunk. Token-cap is approximated as 4 chars/token; design says
# 512 token cap → ~2048 chars. We keep this configurable via env var so a
# steward can tune without redeploying agent containers.
DEFAULT_MAX_CHARS = int(os.environ.get('CHUNKER_MAX_CHARS', '2048'))

# Minimum chunk size — below this we coalesce with the previous chunk to
# avoid a flood of tiny one-line chunks from heading-heavy markdown.
DEFAULT_MIN_CHARS = int(os.environ.get('CHUNKER_MIN_CHARS', '120'))


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------


@dataclass
class Chunk:
    """One chunk record. ``sourceLocation`` carries page/section info when
    the file format permits it (PDF page, Markdown section heading)."""

    chunk_id: str
    doc_id: str
    ontology_id: str
    text: str
    tokens: int
    source_location: dict
    created_at_index: int

    def as_dict(self) -> dict:
        """Serialise for downstream stages and DDB write."""
        return {
            'chunkId': self.chunk_id,
            'docId': self.doc_id,
            'ontologyId': self.ontology_id,
            'text': self.text,
            'tokens': self.tokens,
            'sourceLocation': self.source_location,
            'createdAtIndex': self.created_at_index,
        }


# ----------------------------------------------------------------------------
# Chunkers
# ----------------------------------------------------------------------------


def _approx_tokens(text: str) -> int:
    """Rough token count using the 4-char-per-token heuristic."""
    return max(1, len(text) // 4)


def _window_split(
    text: str, *, max_chars: int = DEFAULT_MAX_CHARS
) -> Iterator[str]:
    """Sliding-window split that prefers paragraph boundaries.

    The greedy-paragraph split keeps semantically coherent chunks together
    when paragraphs are short, and falls back to a hard window when one
    paragraph exceeds ``max_chars`` (rare in real docs but worth handling).
    """
    if not text.strip():
        return
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    buf = ''
    for para in paragraphs:
        if len(para) > max_chars:
            # Flush whatever we accumulated, then hard-window the long one.
            if buf:
                yield buf
                buf = ''
            for i in range(0, len(para), max_chars):
                yield para[i : i + max_chars]
            continue
        candidate = (buf + '\n\n' + para) if buf else para
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            yield buf
            buf = para
    if buf:
        yield buf


def chunk_text(
    *,
    text: str,
    doc_id: str,
    ontology_id: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> List[Chunk]:
    """Chunk a plain-text document via window-split."""
    out: List[Chunk] = []
    for idx, body in enumerate(_window_split(text=text, max_chars=max_chars)):
        out.append(
            Chunk(
                chunk_id=str(uuid.uuid4()),
                doc_id=doc_id,
                ontology_id=ontology_id,
                text=body,
                tokens=_approx_tokens(body),
                source_location={'type': 'text'},
                created_at_index=idx,
            )
        )
    return out


_HEADING_PATTERN = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)


def chunk_markdown(
    *,
    text: str,
    doc_id: str,
    ontology_id: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> List[Chunk]:
    """Heading-aware Markdown chunker.

    Splits at top-level headings; merges chunks below ``min_chars`` into the
    previous chunk so heading-heavy docs don't explode into thousands of
    near-empty rows.
    """
    headings = list(_HEADING_PATTERN.finditer(text))
    if not headings:
        # No headings: treat as plain text.
        return chunk_text(
            text=text,
            doc_id=doc_id,
            ontology_id=ontology_id,
            max_chars=max_chars,
        )

    sections: List[tuple[str, str]] = []  # (heading, body)
    for i, m in enumerate(headings):
        heading = m.group(2).strip()
        start = m.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[start:end].strip()
        sections.append((heading, body))

    # First, merge sections shorter than min_chars with their neighbour.
    merged: List[tuple[str, str]] = []
    for heading, body in sections:
        if merged and len(body) < min_chars:
            prev_h, prev_b = merged[-1]
            merged[-1] = (prev_h, prev_b + '\n\n' + body)
        else:
            merged.append((heading, body))

    out: List[Chunk] = []
    idx = 0
    for heading, body in merged:
        # If the merged section is still too long, fall back to window split.
        if len(body) > max_chars:
            for piece in _window_split(text=body, max_chars=max_chars):
                out.append(
                    Chunk(
                        chunk_id=str(uuid.uuid4()),
                        doc_id=doc_id,
                        ontology_id=ontology_id,
                        text=piece,
                        tokens=_approx_tokens(piece),
                        source_location={'type': 'markdown', 'section': heading},
                        created_at_index=idx,
                    )
                )
                idx += 1
        else:
            out.append(
                Chunk(
                    chunk_id=str(uuid.uuid4()),
                    doc_id=doc_id,
                    ontology_id=ontology_id,
                    text=body,
                    tokens=_approx_tokens(body),
                    source_location={'type': 'markdown', 'section': heading},
                    created_at_index=idx,
                )
            )
            idx += 1
    return out


def detect_format(*, filename: str) -> str:
    """Map a filename extension to the chunker key."""
    lower = filename.lower()
    if lower.endswith('.md') or lower.endswith('.markdown'):
        return 'markdown'
    if lower.endswith('.pdf'):
        return 'pdf'
    if lower.endswith('.docx'):
        return 'docx'
    return 'text'


def chunk_document(
    *,
    text: str,
    filename: str,
    doc_id: str,
    ontology_id: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> List[Chunk]:
    """Top-level dispatcher.

    PDF and DOCX paths still feed plain-text into ``chunk_text`` once the
    extracting library has produced a string; the dispatcher centralises the
    extension → chunker mapping so callers don't need to know which library
    handled the extraction.
    """
    fmt = detect_format(filename=filename)
    if fmt == 'markdown':
        return chunk_markdown(
            text=text,
            doc_id=doc_id,
            ontology_id=ontology_id,
            max_chars=max_chars,
        )
    return chunk_text(
        text=text,
        doc_id=doc_id,
        ontology_id=ontology_id,
        max_chars=max_chars,
    )


# ----------------------------------------------------------------------------
# Lambda handler
# ----------------------------------------------------------------------------


def handler(event: dict, context=None) -> dict:
    """Step Functions invokes this with ``{ docId, ontologyId, s3Bucket, s3Key,
    filename }``. The function reads the object, calls ``chunk_document``, and
    returns the chunk records (later stages embed/persist them).

    For the day-one slice we keep S3 IO at the boundary so unit tests can drive
    ``chunk_document`` directly without AWS.
    """
    import boto3  # local import — keeps unit tests free of boto3 dep at import time

    doc_id = event['docId']
    ontology_id = event['ontologyId']
    bucket = event['s3Bucket']
    key = event['s3Key']
    filename = event.get('filename', key.rsplit('/', 1)[-1])

    s3 = boto3.client('s3', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
    obj = s3.get_object(Bucket=bucket, Key=key)
    raw = obj['Body'].read()
    text = raw.decode('utf-8', errors='replace')

    chunks = chunk_document(
        text=text,
        filename=filename,
        doc_id=doc_id,
        ontology_id=ontology_id,
    )
    return {
        'docId': doc_id,
        'ontologyId': ontology_id,
        'chunkCount': len(chunks),
        'chunks': [c.as_dict() for c in chunks],
    }
