"""Tests for the doc-pipeline chunker (item #3)."""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'doc-pipeline', 'chunker'),
)

from handler import (  # noqa: E402
    chunk_document,
    chunk_markdown,
    chunk_text,
    detect_format,
)


def test_detect_format_by_extension():
    assert detect_format(filename='foo.md') == 'markdown'
    assert detect_format(filename='Foo.MARKDOWN') == 'markdown'
    assert detect_format(filename='foo.pdf') == 'pdf'
    assert detect_format(filename='foo.docx') == 'docx'
    assert detect_format(filename='foo.txt') == 'text'
    assert detect_format(filename='foo.unknown') == 'text'


def test_chunk_text_short_doc_is_one_chunk():
    text = "Short paragraph one.\n\nShort paragraph two."
    chunks = chunk_text(text=text, doc_id='d', ontology_id='o')
    assert len(chunks) == 1
    assert 'paragraph one' in chunks[0].text
    assert chunks[0].source_location == {'type': 'text'}
    assert chunks[0].tokens > 0


def test_chunk_text_splits_when_over_cap():
    paragraphs = ['Para %d. %s' % (i, 'x' * 200) for i in range(20)]
    text = '\n\n'.join(paragraphs)
    chunks = chunk_text(text=text, doc_id='d', ontology_id='o', max_chars=400)
    assert len(chunks) > 1
    assert all(len(c.text) <= 400 for c in chunks)
    # Chunks are indexed in order.
    assert [c.created_at_index for c in chunks] == list(range(len(chunks)))


def test_chunk_text_handles_paragraph_longer_than_cap():
    text = 'x' * 5000
    chunks = chunk_text(text=text, doc_id='d', ontology_id='o', max_chars=1000)
    assert len(chunks) == 5
    assert all(len(c.text) <= 1000 for c in chunks)


def test_chunk_text_skips_empty_input():
    assert chunk_text(text='', doc_id='d', ontology_id='o') == []
    assert chunk_text(text='   \n\n  ', doc_id='d', ontology_id='o') == []


def test_chunk_markdown_uses_headings_for_section():
    text = "# Intro\n\nIntroduction body.\n\n# Details\n\nDetail body line one."
    chunks = chunk_markdown(
        text=text, doc_id='d', ontology_id='o', min_chars=10
    )
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
    chunks = chunk_markdown(
        text=text, doc_id='d', ontology_id='o', min_chars=200
    )
    # Expect at most 2 chunks (A merged with B, then C).
    assert len(chunks) <= 2


def test_chunk_markdown_falls_back_to_window_when_section_too_long():
    long_body = 'word ' * 1000
    text = "# Single section\n\n" + long_body
    chunks = chunk_markdown(
        text=text, doc_id='d', ontology_id='o', max_chars=500
    )
    assert len(chunks) > 1
    assert all(len(c.text) <= 500 for c in chunks)


def test_chunk_markdown_no_headings_falls_through_to_text():
    text = "Just paragraphs.\n\nNo headings here.\n\nAnother paragraph."
    md_chunks = chunk_markdown(text=text, doc_id='d', ontology_id='o')
    txt_chunks = chunk_text(text=text, doc_id='d', ontology_id='o')
    assert len(md_chunks) == len(txt_chunks)


def test_chunk_document_dispatches_on_extension():
    text = "# Heading\n\nbody"
    md = chunk_document(text=text, filename='x.md', doc_id='d', ontology_id='o')
    txt = chunk_document(text=text, filename='x.txt', doc_id='d', ontology_id='o')
    assert md[0].source_location.get('section') == 'Heading'
    assert txt[0].source_location.get('type') == 'text'


def test_chunks_carry_doc_and_ontology_ids():
    chunks = chunk_text(text='hello', doc_id='doc-1', ontology_id='ont-1')
    assert chunks[0].doc_id == 'doc-1'
    assert chunks[0].ontology_id == 'ont-1'
