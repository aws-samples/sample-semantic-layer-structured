"""Convert REST-API chat-history rows into the Strands ``Agent.messages`` shape.

The REST API persists each turn as ``{role, text, turnId, reasoningSteps,
totals?, thinking?}`` in DynamoDB and the ``history_window`` helper returns a
sliding window of those rows on every chat turn. Strands' Bedrock model expects
``[{role, content: [{text: '...'}]}, ...]`` instead, so we map between the two
shapes here.

Token-efficient lazy loading: rather than embed full SQL result rows on every
turn, we append a single-line *pointer* to assistant turns that carry totals.
The pointer announces the turnId, SQL, and row count so the model can:
  * answer "remind me?" / "what was that?" purely from the pointer + prose; or
  * call ``get_previous_query_result(turn_id=...)`` to fetch the full rows on
    demand when the user asks for a specific row or column.

Reasoning steps and thinking text are still dropped — the LLM only needs the
text exchange + lightweight pointer to keep follow-up turns coherent.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_POINTER_PREFIX = "[Prior result]"


def _format_pointer(*, turn_id: str, totals: Dict[str, Any]) -> str:
    """Render a compact one-line pointer for an assistant turn's SQL totals.

    Args:
        turn_id: The turnId of the assistant turn (used as the lookup key).
        totals: The persisted ``run_finished.totals`` dict —
            ``{sql, rowCount, rows, truncated, kbSources, ...}``.

    Returns:
        A human-readable single line such as
        ``[Prior result] turnId=t-abc rows=10 sql=SELECT COUNT(*) FROM admin_codes``.
        Returns an empty string when there is nothing useful to point at.
    """
    sql = (totals.get('sql') or '').strip()
    row_count = totals.get('rowCount')
    if not sql and row_count in (None, 0):
        return ''
    parts: List[str] = [f"{_POINTER_PREFIX} turnId={turn_id}"]
    if isinstance(row_count, int):
        parts.append(f"rows={row_count}")
    if sql:
        # Single-line SQL keeps the pointer compact in the model's context
        # window. Long queries are still useful even when summarized.
        sql_one_line = ' '.join(sql.split())
        parts.append(f"sql={sql_one_line}")
    return ' '.join(parts)


def to_strands_messages(
    history: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Convert a list of persisted chat-history rows into Strands message dicts.

    Args:
        history: Sliding-window list of turns from
            ``ChatSessionService.history_window`` — each row is
            ``{role, text, turnId, reasoningSteps, totals?, thinking?}``.
            ``None`` and empty lists are both treated as no-history.

    Returns:
        A list of ``{'role', 'content': [{'text'}]}`` dicts the Strands Agent
        constructor accepts via the ``messages=`` kwarg. Assistant rows that
        carry SQL totals are augmented with a one-line pointer so the model
        can decide whether to call ``get_previous_query_result`` on follow-ups.
    """
    if not history:
        return []
    out: List[Dict[str, Any]] = []
    for row in history:
        if not isinstance(row, dict):
            continue
        role = row.get('role')
        if role not in ('user', 'assistant'):
            continue
        text = row.get('text') or ''
        if not text.strip():
            continue
        if role == 'assistant':
            totals = row.get('totals')
            turn_id = row.get('turnId') or ''
            if isinstance(totals, dict) and turn_id:
                pointer = _format_pointer(turn_id=turn_id, totals=totals)
                if pointer:
                    text = f"{text}\n\n{pointer}"
        out.append({'role': role, 'content': [{'text': text}]})
    return out
