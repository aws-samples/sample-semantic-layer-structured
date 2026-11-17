# ⚠️ VENDORED COPY — DO NOT EDIT HERE.
# Source of truth: agents/shared/exec_metrics.py. This is a byte-for-byte copy
# committed next to the benchmark-eval Lambda handler so the deployed asset is
# self-contained (the Lambda has no access to the repo's ``agents/`` package).
# Keep it in sync with the canonical module.
"""Execution-based text-to-SQL metrics: EX, R-VES, Soft-F1.

Pure functions over ALREADY-FETCHED result sets (each a list of row-tuples).
Executing predicted vs. gold SQL on Athena is the caller's job (athena_bench);
keeping these pure makes them unit-testable and dialect-agnostic. Conventions
follow the BIRD mini-dev evaluator.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, List, Sequence, Tuple

Row = Tuple


def _norm_cell(cell: Any) -> str:
    """Normalize one result cell to a comparable string.

    Athena returns every cell as a string (``VarCharValue``); gold rows may carry
    Python ints/floats/Decimals/bools. Numeric-looking cells are compared by float
    value rounded to 6 decimals (so ``"1"`` == ``1``, ``"0.50"`` == ``0.5``, and
    aggregate drift like ``0.30000000000000004`` == ``0.3``); booleans normalize to
    lowercase ``"true"``/``"false"`` (Python ``str(True)`` vs Athena ``"true"``);
    everything else compares as a stripped string. This mirrors BIRD's numeric-
    tolerant result comparison.

    Args:
        cell: A single result-set cell value (str/int/float/Decimal/None).

    Returns:
        A canonical string key: ``"None"`` for NULL, a normalized float repr for
        numerics, else the stripped string form.
    """
    if cell is None:
        return "None"
    s = str(cell).strip()
    # Booleans: Athena serializes lowercase "true"/"false"; Python str(True) is
    # "True". Normalize case so a Python-gold bool matches an Athena-string bool.
    if s.lower() in ("true", "false"):
        return s.lower()
    try:
        # Round to a fixed tolerance before keying: predicted values come from
        # Athena (strings) and gold from Python numbers (different engines), so a
        # correct aggregate can drift in the last float digit. Rounding to 1e-6
        # collapses "0.30000000000000004" and "0.3" — matching BIRD's tolerant
        # numeric comparison. round(...)+0.0 normalizes -0.0 to 0.0.
        return repr(round(float(s), 6) + 0.0)
    except (ValueError, OverflowError):
        return s


def _multiset(rows: Sequence[Row]) -> Counter:
    """Return an order-insensitive multiset of normalized rows.

    Args:
        rows: Result rows (tuples of cell values).

    Returns:
        A ``Counter`` keyed by the row's per-cell-normalized tuple, so duplicate
        rows count and numeric/string forms compare equal — but the comparison is
        column-order SENSITIVE within a row (see ``execution_accuracy``).
    """
    return Counter(tuple(_norm_cell(c) for c in r) for r in rows)


def execution_accuracy(*, pred_rows: List[Row], gold_rows: List[Row]) -> int:
    """Execution Accuracy (EX): 1 iff result sets match as multisets.

    Cells are numeric-normalized (see ``_norm_cell``) and rows compare as an
    order-insensitive multiset ACROSS rows. **Within a row, comparison is column-
    order SENSITIVE** and requires the same column count — i.e. ``SELECT a,b`` vs.
    ``SELECT b,a`` scores 0, and selecting extra/fewer columns scores 0. This is
    the strict set-match convention (matches Spider/BIRD's exact-match mode); it is
    deliberately stricter than Soft-F1, which is the tolerant companion metric.

    Args:
        pred_rows: Rows returned by the predicted SQL.
        gold_rows: Rows returned by the gold SQL.

    Returns:
        1 when the normalized multisets are equal, else 0 (BIRD's binary EX).
    """
    return int(_multiset(pred_rows) == _multiset(gold_rows))


def reward_ves(
    *, pred_rows: List[Row], gold_rows: List[Row], pred_ms: float, gold_ms: float
) -> float:
    """Reward-based Valid Efficiency Score (R-VES), per BIRD's reward bands.

    Zero unless the result is correct (EX == 1); otherwise a reward band on the
    ``gold_ms / pred_ms`` time ratio (higher => prediction faster). Bands mirror
    BIRD mini-dev's ``evaluation_ves.py`` reward function, INCLUDING the >1.0 top
    band that credits a prediction >=2x faster than gold.

    NOTE (executor): these thresholds/rewards should be verified against BIRD's
    published evaluation_ves.py at the pinned release tag before the number is
    trusted as leaderboard-comparable; the timing SOURCE (Athena engine time) also
    differs from BIRD's SQLite timing. Keep the bands as written for now.

    Args:
        pred_rows: Rows from the predicted SQL.
        gold_rows: Rows from the gold SQL.
        pred_ms: Predicted-query wall time in milliseconds (> 0).
        gold_ms: Gold-query wall time in milliseconds (> 0).

    Returns:
        A reward in {0.0, 0.25, 0.5, 0.75, 1.0, 1.25}.

    Raises:
        ValueError: A timing is non-positive (fail loudly — a zero time is a
            measurement bug, not "infinitely fast").
    """
    if pred_ms <= 0 or gold_ms <= 0:
        raise ValueError(f"timings must be > 0 (pred={pred_ms}, gold={gold_ms})")
    if execution_accuracy(pred_rows=pred_rows, gold_rows=gold_rows) == 0:
        return 0.0
    ratio = gold_ms / pred_ms  # > 1 => prediction faster than gold
    if ratio >= 2.0:
        return 1.25
    if ratio >= 1.0:
        return 1.0
    if ratio >= 0.5:
        return 0.75
    if ratio >= 0.25:
        return 0.5
    return 0.25


def soft_f1(*, pred_rows: List[Row], gold_rows: List[Row]) -> float:
    """Soft-F1 over result rows as multisets (order/duplicate tolerant).

    Args:
        pred_rows: Rows from the predicted SQL.
        gold_rows: Rows from the gold SQL.

    Returns:
        Harmonic mean of precision/recall on matched rows in [0.0, 1.0]. Two
        empty result sets score 1.0 (both correctly returned nothing).
    """
    pred, gold = _multiset(pred_rows), _multiset(gold_rows)
    if not pred and not gold:
        return 1.0
    overlap = sum((pred & gold).values())
    if overlap == 0:
        return 0.0
    precision = overlap / sum(pred.values())
    recall = overlap / sum(gold.values())
    return 2 * precision * recall / (precision + recall)
