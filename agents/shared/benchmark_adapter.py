"""Benchmark row model + loader/writer.

Normalizes every external benchmark (BIRD, CWD, Spider 2.0) and the legacy
insurance ground-truth file into a single ``BenchmarkRow`` shape so eval
notebooks and metric scorers never branch on the source dataset. Per the design,
result sets are NOT stored — gold SQL is executed at eval time — so the writer
never emits ``Expected_SQL_Result``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class BenchmarkRow:
    """One normalized benchmark example.

    Args:
        question: The natural-language question fed to the agent.
        gold_sql: The reference SQL (empty for advisory/meta rows).
        db_id: Source database identifier (empty for the legacy single-DB set).
        difficulty: Optional BIRD difficulty band ("simple"/"moderate"/"challenging").
        evidence: Optional oracle-knowledge hint (BIRD "evidence").
        expected_tier: Optional tier tag from the legacy insurance rows.
        expected_answer: Optional NL answer (legacy rows / advisory).
    """

    question: str
    gold_sql: str = ""
    db_id: str = ""
    difficulty: str = ""
    evidence: str = ""
    expected_tier: str = ""
    expected_answer: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BenchmarkRow":
        """Build a row from a raw dict, requiring a question.

        Args:
            data: Raw record using the ground-truth field names.

        Returns:
            A populated ``BenchmarkRow``.

        Raises:
            ValueError: The record has no ``Natural_Language_Question``.
        """
        q = data.get("Natural_Language_Question")
        if not q:
            raise ValueError("row missing required 'Natural_Language_Question'")
        return cls(
            question=q,
            gold_sql=data.get("Expected_SQL_Query", ""),
            db_id=data.get("db_id", ""),
            difficulty=data.get("difficulty", ""),
            evidence=data.get("evidence", ""),
            expected_tier=data.get("Expected_Tier", ""),
            expected_answer=data.get("Expected_Answer", ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the ground-truth field names, omitting empty extras.

        Returns:
            A dict with ``Natural_Language_Question`` + ``Expected_SQL_Query``
            always present and non-empty optional fields included. Never emits
            ``Expected_SQL_Result``.
        """
        out: Dict[str, Any] = {
            "Natural_Language_Question": self.question,
            "Expected_SQL_Query": self.gold_sql,
        }
        for key, val in (
            ("db_id", self.db_id),
            ("difficulty", self.difficulty),
            ("evidence", self.evidence),
            ("Expected_Tier", self.expected_tier),
            ("Expected_Answer", self.expected_answer),
        ):
            if val:
                out[key] = val
        return out


def load_rows(*, path: Path) -> List[BenchmarkRow]:
    """Load rows from a legacy JSON list or a per-benchmark JSONL file.

    Args:
        path: File ending in ``.json`` (list-of-dict) or ``.jsonl`` (one dict
            per line).

    Returns:
        The parsed rows in file order.

    Raises:
        ValueError: The suffix is neither ``.json`` nor ``.jsonl``.
    """
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith(".jsonl"):
        records = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    elif str(path).endswith(".json"):
        records = json.loads(text)
    else:
        raise ValueError(f"unsupported dataset suffix: {path}")
    return [BenchmarkRow.from_dict(r) for r in records]


def write_jsonl(*, rows: List[BenchmarkRow], path: Path) -> None:
    """Write rows as JSONL (one record per line).

    Args:
        rows: Rows to serialize.
        path: Destination ``.jsonl`` path (parent dirs created).

    Returns:
        None.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row.to_dict()) + "\n")
