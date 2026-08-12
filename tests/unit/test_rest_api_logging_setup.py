"""Regression tests for the REST API root-logger setup in ``lambda/rest-api/main.py``.

The cold-start block clears pre-existing handlers from the root logger to avoid
duplicate CloudWatch log lines (AWS Lambda installs its own root handler). The
original implementation iterated the live ``root_logger.handlers`` list while
calling ``removeHandler`` on it, which mutates the list mid-iteration and skips
elements — leaving stale handlers behind and defeating the de-duplication.
"""
import logging
import re
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[2] / "lambda" / "rest-api" / "main.py"


def test_iterating_live_handlers_while_removing_skips_handlers():
    """Document the underlying stdlib behaviour the fix must avoid."""
    demo = logging.getLogger("rest_api_logging_setup_demo_buggy")
    demo.handlers = []
    for _ in range(3):
        demo.addHandler(logging.NullHandler())

    # Buggy pattern: mutating the list being iterated skips elements.
    for handler in demo.handlers:
        demo.removeHandler(handler)

    assert len(demo.handlers) > 0  # handlers survive the buggy loop

    # Correct pattern: iterate over a snapshot copy.
    for handler in list(demo.handlers):
        demo.removeHandler(handler)
    assert demo.handlers == []


def test_main_clears_all_handlers_via_list_copy():
    """The real source must iterate over a copy of the handler list."""
    source = MAIN_PY.read_text()

    # The correct, copy-based removal must be present.
    assert re.search(
        r"for\s+\w+\s+in\s+list\(root_logger\.handlers\)\s*:", source
    ), "expected handler removal to iterate over list(root_logger.handlers)"

    # The buggy live-iteration pattern must not be present.
    assert not re.search(
        r"for\s+\w+\s+in\s+root_logger\.handlers\s*:", source
    ), "handler removal must not iterate the live root_logger.handlers list"
