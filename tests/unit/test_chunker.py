"""Tests for the doc-pipeline chunker (item #3).

Rewritten for the extract→chunk split introduced when the chunker gained real
PDF/DOCX extraction: the old ``chunk_text`` / ``chunk_markdown`` /
``chunk_document(text=...)`` API was replaced by ``extract_blocks`` (bytes →
provenance-carrying blocks) followed by ``chunk_blocks`` (blocks → chunks). The
behaviours asserted here are the SAME ones the previous tests pinned — cap
enforcement, section provenance, short-section merging, ordering — expressed
against the current API.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'doc-pipeline', 'chunker'),
)

from handler import (  # noqa: E402
    ExtractionError,
    chunk_blocks,
    chunk_document,
    detect_format,
    extract_blocks,
    extract_markdown_blocks,
)


def _chunk_text(text: str, *, filename: str = 'x.txt', **kwargs):
    """Extract + chunk a plain-text string, the old ``chunk_text`` shorthand.

    Args:
        text: The document body.
        filename: Drives extractor selection (``.txt`` → the text extractor).
        **kwargs: Passed through to ``chunk_blocks`` (e.g. ``max_chars``).

    Returns:
        The resulting list of ``Chunk`` objects.
    """
    blocks = extract_blocks(raw=text.encode('utf-8'), filename=filename)
    return chunk_blocks(blocks=blocks, doc_id='d', ontology_id='o', **kwargs)


def _chunk_markdown(text: str, *, min_chars: int | None = None, **kwargs):
    """Extract + chunk markdown, the old ``chunk_markdown`` shorthand.

    Args:
        text: The markdown source.
        min_chars: Short-section merge threshold; omitted uses the default.
        **kwargs: Passed through to ``chunk_blocks``.

    Returns:
        The resulting list of ``Chunk`` objects.
    """
    blocks = (
        extract_markdown_blocks(text=text, min_chars=min_chars)
        if min_chars is not None
        else extract_markdown_blocks(text=text)
    )
    return chunk_blocks(blocks=blocks, doc_id='d', ontology_id='o', **kwargs)


def test_detect_format_by_extension():
    assert detect_format(filename='foo.md') == 'markdown'
    assert detect_format(filename='Foo.MARKDOWN') == 'markdown'
    assert detect_format(filename='foo.pdf') == 'pdf'
    assert detect_format(filename='foo.docx') == 'docx'
    assert detect_format(filename='foo.txt') == 'text'
    assert detect_format(filename='foo.unknown') == 'text'


def test_chunk_text_short_doc_is_one_chunk():
    text = "Short paragraph one.\n\nShort paragraph two."
    chunks = _chunk_text(text)
    assert len(chunks) == 1
    assert 'paragraph one' in chunks[0].text
    assert chunks[0].source_location == {'type': 'text'}
    assert chunks[0].tokens > 0


def test_chunk_text_splits_when_over_cap():
    paragraphs = ['Para %d. %s' % (i, 'x' * 200) for i in range(20)]
    text = '\n\n'.join(paragraphs)
    chunks = _chunk_text(text, max_chars=400)
    assert len(chunks) > 1
    assert all(len(c.text) <= 400 for c in chunks)
    # Chunks are indexed in order.
    assert [c.created_at_index for c in chunks] == list(range(len(chunks)))


def test_chunk_text_handles_paragraph_longer_than_cap():
    text = 'x' * 5000
    chunks = _chunk_text(text, max_chars=1000)
    assert len(chunks) == 5
    assert all(len(c.text) <= 1000 for c in chunks)


def test_extraction_of_empty_input_raises():
    """An empty document is an ERROR, not an empty chunk list.

    The old chunker returned ``[]`` here, which flowed through every downstream
    stage and produced a KB document that ingested cleanly and answered nothing.
    """
    for body in ('', '   \n\n  '):
        try:
            extract_blocks(raw=body.encode('utf-8'), filename='x.txt')
        except ExtractionError:
            continue
        raise AssertionError(f'empty input {body!r} did not raise ExtractionError')


def test_chunk_markdown_uses_headings_for_section():
    text = "# Intro\n\nIntroduction body.\n\n# Details\n\nDetail body line one."
    chunks = _chunk_markdown(text, min_chars=10)
    sections = [c.source_location.get('section') for c in chunks]
    assert 'Intro' in sections
    assert 'Details' in sections


def test_chunk_markdown_merges_short_sections():
    # Three headings; the first two have very short bodies and should merge.
    text = (
        "# A\n\nshort\n\n"
        "# B\n\nshort\n\n"
        "# C\n\n" + ('long body. ' * 30)
    )
    chunks = _chunk_markdown(text, min_chars=200)
    # Expect at most 2 chunks (A merged with B, then C).
    assert len(chunks) <= 2


def test_chunk_markdown_falls_back_to_window_when_section_too_long():
    long_body = 'word ' * 1000
    text = "# Single section\n\n" + long_body
    chunks = _chunk_markdown(text, max_chars=500)
    assert len(chunks) > 1
    assert all(len(c.text) <= 500 for c in chunks)


def test_chunk_markdown_no_headings_still_yields_one_block():
    text = "Just paragraphs.\n\nNo headings here.\n\nAnother paragraph."
    md_chunks = _chunk_markdown(text)
    txt_chunks = _chunk_text(text)
    assert len(md_chunks) == len(txt_chunks) == 1
    # The markdown extractor still stamps the format, so a citation label can
    # tell a heading-less markdown file from a .txt one.
    assert md_chunks[0].source_location == {'type': 'markdown'}


def test_chunk_document_dispatches_on_extension():
    text = "# Heading\n\nbody"
    md = chunk_document(
        raw=text.encode('utf-8'), filename='x.md', doc_id='d', ontology_id='o'
    )
    txt = chunk_document(
        raw=text.encode('utf-8'), filename='x.txt', doc_id='d', ontology_id='o'
    )
    assert md[0].source_location.get('section') == 'Heading'
    assert txt[0].source_location.get('type') == 'text'


def test_chunks_carry_doc_and_ontology_ids():
    chunks = chunk_document(
        raw=b'hello', filename='x.txt', doc_id='doc-1', ontology_id='ont-1'
    )
    assert chunks[0].doc_id == 'doc-1'
    assert chunks[0].ontology_id == 'ont-1'
