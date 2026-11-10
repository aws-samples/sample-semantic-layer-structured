"""Unit tests for EX / R-VES / Soft-F1 execution metrics."""

from __future__ import annotations

from agents.shared.exec_metrics import (
    execution_accuracy,
    reward_ves,
    soft_f1,
)

GOLD = [(1, "a"), (2, "b"), (3, "c")]


def test_ex_true_when_same_rows_any_order():
    assert (
        execution_accuracy(pred_rows=[(3, "c"), (1, "a"), (2, "b")], gold_rows=GOLD)
        == 1
    )


def test_ex_false_when_rows_differ():
    assert execution_accuracy(pred_rows=[(1, "a"), (2, "b")], gold_rows=GOLD) == 0


def test_rves_zero_when_result_wrong():
    # No efficiency reward for an incorrect query, regardless of speed.
    assert (
        reward_ves(pred_rows=[(1, "a")], gold_rows=GOLD, pred_ms=1.0, gold_ms=100.0)
        == 0.0
    )


def test_rves_rewards_correct_and_same_speed():
    # Correct + same speed as gold (ratio 1.0) => base full band (1.0).
    r = reward_ves(pred_rows=GOLD, gold_rows=GOLD, pred_ms=100.0, gold_ms=100.0)
    assert r == 1.0


def test_rves_top_band_when_much_faster():
    # Correct + >=2x faster than gold (ratio 2.0) => top band (1.25), per BIRD.
    r = reward_ves(pred_rows=GOLD, gold_rows=GOLD, pred_ms=50.0, gold_ms=100.0)
    assert r == 1.25


def test_soft_f1_perfect_match_is_one():
    assert soft_f1(pred_rows=GOLD, gold_rows=GOLD) == 1.0


def test_soft_f1_partial_overlap_between_zero_and_one():
    score = soft_f1(pred_rows=[(1, "a"), (2, "b"), (9, "z")], gold_rows=GOLD)
    assert 0.0 < score < 1.0


def test_soft_f1_empty_both_is_one():
    # Two empty result sets agree perfectly (both correctly returned nothing).
    assert soft_f1(pred_rows=[], gold_rows=[]) == 1.0


def test_soft_f1_pred_empty_gold_nonempty_is_zero():
    assert soft_f1(pred_rows=[], gold_rows=GOLD) == 0.0


def test_ex_numeric_normalization_int_vs_string():
    # Athena returns cells as strings; gold rows may be ints/decimals. EX must
    # normalize numerics so "1" == 1 and "0.50" == 0.5 (BIRD convention).
    assert (
        execution_accuracy(pred_rows=[("1",), ("0.50",)], gold_rows=[(1,), (0.5,)]) == 1
    )


def test_ex_duplicate_rows_are_multiset_sensitive():
    # [a, a] != [a] -- duplicates count (multiset, not set).
    assert execution_accuracy(pred_rows=[(1,), (1,)], gold_rows=[(1,)]) == 0


def test_rves_zero_timing_raises():
    import pytest

    with pytest.raises(ValueError, match="> 0"):
        reward_ves(pred_rows=GOLD, gold_rows=GOLD, pred_ms=0, gold_ms=100.0)


def test_rves_intermediate_bands():
    # Boundary transitions in the >= chain (ratio = gold_ms / pred_ms).
    assert (
        reward_ves(pred_rows=GOLD, gold_rows=GOLD, pred_ms=100.0, gold_ms=75.0) == 0.75
    )  # 0.75
    assert (
        reward_ves(pred_rows=GOLD, gold_rows=GOLD, pred_ms=100.0, gold_ms=50.0) == 0.75
    )  # exactly 0.5 -> 0.75
    assert (
        reward_ves(pred_rows=GOLD, gold_rows=GOLD, pred_ms=100.0, gold_ms=40.0) == 0.5
    )  # 0.4  -> 0.5
    assert (
        reward_ves(pred_rows=GOLD, gold_rows=GOLD, pred_ms=100.0, gold_ms=25.0) == 0.5
    )  # exactly 0.25 -> 0.5
    assert (
        reward_ves(pred_rows=GOLD, gold_rows=GOLD, pred_ms=100.0, gold_ms=10.0) == 0.25
    )  # 0.1  -> 0.25


def test_ex_column_order_sensitive_within_row():
    # SELECT a,b vs SELECT b,a must NOT match (documented strict behavior).
    assert execution_accuracy(pred_rows=[("a", 1)], gold_rows=[(1, "a")]) == 0


def test_ex_float_tolerance_collapses_last_digit_drift():
    # 0.1+0.2 == 0.30000000000000004 must match gold 0.3 (aggregate float drift).
    assert execution_accuracy(pred_rows=[(0.1 + 0.2,)], gold_rows=[(0.3,)]) == 1


def test_norm_handles_null_and_bool():
    # NULL -> "None"; Python bool matches Athena lowercase string form.
    assert execution_accuracy(pred_rows=[(None,)], gold_rows=[(None,)]) == 1
    assert execution_accuracy(pred_rows=[("true",)], gold_rows=[(True,)]) == 1
