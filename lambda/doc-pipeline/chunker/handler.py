"""Doc-pipeline chunker (item #3).

Reads a document from S3, extracts text blocks per the file format, splits them
into chunks, and emits chunk records the rest of the pipeline (NER → embedder →
linker → ruleminer → indexer) consumes.

Supported types:
    * Plain text (.txt) — sliding-window token cap, one block.
    * Markdown (.md) — heading-aware split, falling back to window.
    * PDF (.pdf) — page-by-page extraction via pypdf; one block per page.
    * DOCX (.docx) — paragraph extraction via stdlib zipfile + ElementTree;
      one block per heading-delimited run of paragraphs.

Extraction is deliberately **block-aware**: every chunk carries the real
``source_location`` of the text it came from (PDF page number, DOCX block index,
Markdown section heading). The indexer renders those locations into the KB
document as section headings, which is what makes a document citation able to
name a page rather than just a file.

The extractors and chunkers stay pure functions so unit tests can run on local
fixtures without S3.
"""

from __future__ import annotations

import io
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List

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


class ExtractionError(RuntimeError):
    """Raised when a document's text cannot be extracted.

    Deliberately loud. The previous implementation decoded every format as
    UTF-8 with ``errors='replace'``, so a PDF produced mojibake chunks and the
    pipeline reported success — the document was silently useless.
    """


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------


@dataclass
class TextBlock:
    """One extracted region of a document with its provenance.

    Attributes:
        text: The block's plain text.
        source_location: Provenance dict, e.g. ``{'type': 'pdf', 'page': 12}``.
            Carried through to every chunk derived from this block.
    """

    text: str
    source_location: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    """One chunk record. ``source_location`` carries page/section info when
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
# Format detection
# ----------------------------------------------------------------------------


def detect_format(*, filename: str) -> str:
    """Map a filename extension to the extractor key.

    Args:
        filename: The original upload filename (extension is all we use).

    Returns:
        One of ``'markdown'``, ``'pdf'``, ``'docx'``, ``'text'``.
    """
    lower = filename.lower()
    if lower.endswith('.md') or lower.endswith('.markdown'):
        return 'markdown'
    if lower.endswith('.pdf'):
        return 'pdf'
    if lower.endswith('.docx'):
        return 'docx'
    return 'text'


# ----------------------------------------------------------------------------
# Extractors — bytes → [TextBlock]
# ----------------------------------------------------------------------------


def _decode_strict(*, raw: bytes, filename: str) -> str:
    """Decode text-format bytes as UTF-8, raising on invalid sequences.

    Args:
        raw: Raw object bytes.
        filename: Used only for the error message.

    Returns:
        The decoded string.

    Raises:
        ExtractionError: When the bytes are not valid UTF-8. This is the guard
            that replaced ``errors='replace'`` — a mis-detected binary file now
            fails the stage instead of ingesting mojibake.
    """
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ExtractionError(
            f"{filename!r} is not valid UTF-8 text (byte {exc.start}); "
            f"if it is a PDF or DOCX, upload it with the correct extension"
        ) from exc


def extract_pdf_blocks(*, raw: bytes, filename: str) -> List[TextBlock]:
    """Extract one :class:`TextBlock` per PDF page via pypdf.

    Args:
        raw: Raw PDF bytes.
        filename: Used in error messages and page provenance.

    Returns:
        One block per page that yielded text, in page order. Pages with no
        extractable text are skipped (a cover page is normal); a PDF where
        *no* page yields text is an error, not an empty document.

    Raises:
        ExtractionError: When pypdf cannot parse the file, or when the whole
            document yields no text (typically a scanned/image-only PDF, which
            needs OCR — deliberately out of scope rather than silently empty).
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover — packaging guard
        raise ExtractionError(
            "pypdf is not available in the chunker bundle; PDF support requires "
            "the bundled asset (see doc-pipeline-stack.ts bundling block)"
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
        page_texts = [(idx, page.extract_text() or '')
                      for idx, page in enumerate(reader.pages, start=1)]
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 — any pypdf failure is an extraction failure
        raise ExtractionError(f"could not parse PDF {filename!r}: {exc}") from exc

    blocks = [
        TextBlock(text=text.strip(), source_location={'type': 'pdf', 'page': page_no})
        for page_no, text in page_texts
        if text.strip()
    ]
    if not blocks:
        raise ExtractionError(
            f"{filename!r} yielded no extractable text across "
            f"{len(page_texts)} page(s) — it is most likely a scanned "
            f"(image-only) PDF, which needs OCR before upload"
        )
    return blocks


# DOCX is a zip archive of XML parts; the body lives in word/document.xml and
# everything we need is in the WordprocessingML main namespace.
_DOCX_BODY_PART = 'word/document.xml'
_W_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def _docx_paragraphs(*, raw: bytes, filename: str) -> List[tuple[str, str]]:
    """Read (style_name, text) for every paragraph of a DOCX using stdlib only.

    DOCX is deliberately parsed with ``zipfile`` + ``ElementTree`` rather than
    ``python-docx``: python-docx depends on ``lxml``, whose wheel is
    architecture-specific, and the CDK asset's ``local.tryBundle`` runs pip on
    the developer's x86_64 machine for an ARM64 Lambda. Keeping DOCX on the
    stdlib means the bundle needs exactly one pure-Python dependency (pypdf).

    Args:
        raw: Raw DOCX (zip) bytes.
        filename: Used in error messages only.

    Returns:
        ``(style_name, text)`` per paragraph in document order. ``style_name``
        is ``''`` when the paragraph declares no style (Word's default body).

    Raises:
        ExtractionError: When the archive or its body XML cannot be read.
    """
    import xml.etree.ElementTree as ET
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            body_xml = archive.read(_DOCX_BODY_PART)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ExtractionError(
            f"could not read DOCX {filename!r}: not a valid .docx archive "
            f"(missing {_DOCX_BODY_PART}): {exc}"
        ) from exc

    try:
        root = ET.fromstring(body_xml)
    except ET.ParseError as exc:
        raise ExtractionError(
            f"could not parse DOCX {filename!r}: malformed {_DOCX_BODY_PART}: {exc}"
        ) from exc

    paragraphs: List[tuple[str, str]] = []
    for para in root.iter(f'{_W_NS}p'):
        # Paragraph style, when set, is w:pPr/w:pStyle/@w:val ("Heading1", "Title").
        style_el = para.find(f'{_W_NS}pPr/{_W_NS}pStyle')
        style_name = style_el.get(f'{_W_NS}val', '') if style_el is not None else ''
        # Paragraph text is the concatenation of every w:t run inside it.
        text = ''.join(node.text or '' for node in para.iter(f'{_W_NS}t'))
        paragraphs.append((style_name, text))
    return paragraphs


def extract_docx_blocks(*, raw: bytes, filename: str) -> List[TextBlock]:
    """Extract :class:`TextBlock` objects from a DOCX.

    Consecutive body paragraphs are grouped into one block, and a new block is
    started at each Word heading style, so blocks track the document's own
    section structure rather than being one-per-paragraph.

    Args:
        raw: Raw DOCX bytes.
        filename: Used in error messages.

    Returns:
        One block per heading-delimited section (or one block for the whole
        body when the document uses no heading styles).

    Raises:
        ExtractionError: When the file cannot be parsed as a DOCX, or when the
            document contains no text.
    """
    paragraphs = _docx_paragraphs(raw=raw, filename=filename)

    blocks: List[TextBlock] = []
    current: List[str] = []
    current_heading = ''
    block_index = 1

    def _flush() -> None:
        """Emit the accumulated paragraphs as one block, if non-empty."""
        nonlocal current, block_index
        body = '\n\n'.join(line for line in current if line.strip())
        if body.strip():
            location: Dict[str, Any] = {'type': 'docx', 'block': block_index}
            if current_heading:
                location['section'] = current_heading
            blocks.append(TextBlock(text=body.strip(), source_location=location))
            block_index += 1
        current = []

    for style_name, text in paragraphs:
        # Word marks headings with styles named "Heading 1", "Title", etc.
        is_heading = style_name.startswith('Heading') or style_name == 'Title'
        if is_heading and text.strip():
            _flush()
            current_heading = text.strip()
            current.append(text)
            continue
        current.append(text)
    _flush()

    if not blocks:
        raise ExtractionError(f"{filename!r} contains no extractable text")
    return blocks


_HEADING_PATTERN = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)


def extract_markdown_blocks(*, text: str, min_chars: int = DEFAULT_MIN_CHARS
                            ) -> List[TextBlock]:
    """Split Markdown into one :class:`TextBlock` per top-level section.

    Sections shorter than ``min_chars`` are merged into the previous section so
    heading-heavy docs don't explode into thousands of near-empty blocks.

    Args:
        text: The full markdown source.
        min_chars: Sections below this length merge into their predecessor.

    Returns:
        One block per (merged) section, or a single untyped block when the
        document has no headings.
    """
    headings = list(_HEADING_PATTERN.finditer(text))
    if not headings:
        return [TextBlock(text=text.strip(), source_location={'type': 'markdown'})]

    sections: List[tuple[str, str]] = []
    for i, match in enumerate(headings):
        heading = match.group(2).strip()
        start = match.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        sections.append((heading, text[start:end].strip()))

    merged: List[tuple[str, str]] = []
    for heading, body in sections:
        if merged and len(body) < min_chars:
            prev_heading, prev_body = merged[-1]
            merged[-1] = (prev_heading, prev_body + '\n\n' + body)
        else:
            merged.append((heading, body))

    return [
        TextBlock(text=body, source_location={'type': 'markdown', 'section': heading})
        for heading, body in merged
        if body.strip()
    ]


def extract_blocks(*, raw: bytes, filename: str) -> List[TextBlock]:
    """Top-level extractor: raw bytes → provenance-carrying text blocks.

    Args:
        raw: Raw object bytes as read from S3.
        filename: Original filename; its extension selects the extractor.

    Returns:
        The document's text blocks in document order.

    Raises:
        ExtractionError: When the format's extractor fails or yields no text.
    """
    fmt = detect_format(filename=filename)
    if fmt == 'pdf':
        return extract_pdf_blocks(raw=raw, filename=filename)
    if fmt == 'docx':
        return extract_docx_blocks(raw=raw, filename=filename)
    text = _decode_strict(raw=raw, filename=filename)
    if not text.strip():
        raise ExtractionError(f"{filename!r} is empty")
    if fmt == 'markdown':
        return extract_markdown_blocks(text=text)
    return [TextBlock(text=text.strip(), source_location={'type': 'text'})]


# ----------------------------------------------------------------------------
# Chunkers — [TextBlock] → [Chunk]
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


def chunk_blocks(
    *,
    blocks: List[TextBlock],
    doc_id: str,
    ontology_id: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> List[Chunk]:
    """Window-split every block, preserving each block's ``source_location``.

    Args:
        blocks: Extracted blocks in document order.
        doc_id: The document id every chunk belongs to.
        ontology_id: The semantic-layer id scoping the document.
        max_chars: Chunk size cap in characters.

    Returns:
        Chunks in document order, each stamped with its block's provenance.
    """
    out: List[Chunk] = []
    idx = 0
    for block in blocks:
        for piece in _window_split(text=block.text, max_chars=max_chars):
            out.append(
                Chunk(
                    chunk_id=str(uuid.uuid4()),
                    doc_id=doc_id,
                    ontology_id=ontology_id,
                    text=piece,
                    tokens=_approx_tokens(piece),
                    source_location=dict(block.source_location),
                    created_at_index=idx,
                )
            )
            idx += 1
    return out


def chunk_document(
    *,
    raw: bytes,
    filename: str,
    doc_id: str,
    ontology_id: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> List[Chunk]:
    """Extract and chunk one document end to end.

    Args:
        raw: Raw object bytes as read from S3.
        filename: Original filename; its extension selects the extractor.
        doc_id: The document id every chunk belongs to.
        ontology_id: The semantic-layer id scoping the document.
        max_chars: Chunk size cap in characters.

    Returns:
        The document's chunks, each carrying real source provenance.

    Raises:
        ExtractionError: When text cannot be extracted from the document.
    """
    blocks = extract_blocks(raw=raw, filename=filename)
    return chunk_blocks(
        blocks=blocks,
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

    S3 IO stays at the boundary so unit tests can drive ``chunk_document``
    directly without AWS.

    Args:
        event: Step Functions state input (see above).
        context: Unused Lambda context.

    Returns:
        ``{docId, ontologyId, filename, chunkCount, chunks}``. ``filename`` is
        echoed because the indexer needs it to title the KB document.

    Raises:
        ExtractionError: Propagated so the state machine's retry/failure path
            records an unreadable document instead of ingesting garbage.
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

    chunks = chunk_document(
        raw=raw,
        filename=filename,
        doc_id=doc_id,
        ontology_id=ontology_id,
    )
    logger.info(
        'Chunked %s (%d bytes) into %d chunk(s) for layer %s',
        filename, len(raw), len(chunks), ontology_id,
    )
    return {
        'docId': doc_id,
        'ontologyId': ontology_id,
        'filename': filename,
        'chunkCount': len(chunks),
        'chunks': [c.as_dict() for c in chunks],
    }
