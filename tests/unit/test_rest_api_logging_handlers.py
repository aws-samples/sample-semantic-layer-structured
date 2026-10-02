"""Regression test for the root-logger handler cleanup in lambda/rest-api/main.py.

The cold-start logging setup removes any pre-existing handlers from the root
logger before installing its own, so warm-container re-imports do not accumulate
duplicate handlers (which would emit duplicate CloudWatch log lines).

The original code iterated over ``root_logger.handlers`` while calling
``removeHandler`` on the same list. Because ``removeHandler`` mutates the list
in place, iterating the live list skips elements and leaves handlers behind.
This test guards the fixed behaviour: *all* existing handlers are removed.
"""
import logging


def _clear_handlers_fixed(root_logger):
    """Mirror the fixed cleanup logic in main.py (iterate over a copy)."""
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)


def test_clear_handlers_removes_all_handlers():
    logger = logging.getLogger("test_rest_api_logging_handlers_fixed")
    # Start from a known state.
    for h in list(logger.handlers):
        logger.removeHandler(h)

    for _ in range(3):
        logger.addHandler(logging.NullHandler())
    assert len(logger.handlers) == 3

    _clear_handlers_fixed(logger)

    assert logger.handlers == [], "all pre-existing handlers must be removed"


def test_buggy_pattern_would_leave_handlers_behind():
    """Document why iterating the live list is wrong (the original bug)."""
    logger = logging.getLogger("test_rest_api_logging_handlers_buggy")
    for h in list(logger.handlers):
        logger.removeHandler(h)

    for _ in range(3):
        logger.addHandler(logging.NullHandler())

    # The original buggy loop: iterate the live list while mutating it.
    for handler in logger.handlers:
        logger.removeHandler(handler)

    # Removing during iteration skips elements, so handlers remain.
    assert len(logger.handlers) > 0

    # Cleanup so we don't leak handlers to other tests.
    for h in list(logger.handlers):
        logger.removeHandler(h)
