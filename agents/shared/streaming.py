"""Shared streaming helpers for assistant-message delivery.

Utilities used by the query agents when streaming the final answer back to the
frontend as incremental deltas.
"""

from typing import Iterator


def chunk_text(text: str, *, max_chars: int = 80) -> Iterator[str]:
    """Split a string into fixed-size chunks for streaming.

    The frontend treats deltas as opaque concatenable strings, so chunk size
    is purely a UX knob. Empty input yields an empty iterator.

    Args:
        text: The full assistant message to stream.
        max_chars: Maximum characters per emitted chunk.

    Returns:
        An iterator over successive substrings of ``text``.
    """
    if not text:
        return
    for i in range(0, len(text), max_chars):
        yield text[i : i + max_chars]
