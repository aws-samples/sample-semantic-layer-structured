"""Two-phase orchestration contract for the Hybrid creation agent.

``main._build_hybrid`` drives the buildingKB -> buildingVKG -> completed status
sequence. On a KB build failure it records ``failed:kb`` (and does NOT re-raise
from ``_build_hybrid`` itself — propagation happens at the entrypoint layer),
so the happy path yields exactly three statuses while a KB boom ends on a
``failed`` status.

The entrypoint tests below pin the ASYNC contract: a real 40-table build runs far
longer than AgentCore keeps a synchronous invocation alive, so ``invoke`` must hand
off to a background thread and return ``processing`` promptly. Doing the work inline
got the container reaped mid-build with no traceback and no terminal status.
"""
import threading
import types

from agents.semantic_layer_creation_agent import main


def test_two_phase_status_transitions():
    seen = []
    main._build_hybrid(id="o", write_status=lambda s: seen.append(s),
                       build_kb=lambda: None, build_vkg=lambda: None)
    assert seen == ["buildingKB", "buildingVKG", "completed"]


def test_kb_failure_marks_failed():
    seen = []

    def boom(): raise RuntimeError("kb down")
    main._build_hybrid(id="o", write_status=lambda s: seen.append(s),
                       build_kb=boom, build_vkg=lambda: None)
    assert seen[-1].startswith("failed")


def _stub_entrypoint_deps(monkeypatch, *, run_build):
    """Stub out everything ``invoke`` touches except the threading hand-off.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        run_build: The callable to substitute for ``main._run_build``.

    Returns:
        None.
    """
    monkeypatch.setattr(main, "_read_config", lambda id: {"dataSources": [{}]})
    monkeypatch.setattr(main, "_metadata_table", lambda: object())
    monkeypatch.setattr(main, "_resolve_active_version", lambda table, id: "v1")
    monkeypatch.setattr(
        main, "_tables_from_config", lambda config: [{"table": "t"}]
    )
    monkeypatch.setattr(main, "_run_build", run_build)


def test_invoke_returns_processing_without_waiting_for_the_build(monkeypatch):
    """``invoke`` must return before the build finishes, or the container is reaped."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_build(**kwargs):
        started.set()
        # Block until the assertions below have run, standing in for the many
        # minutes a real build spends in the KB phase.
        release.wait(timeout=5)
        finished.set()
        return {"status": "completed"}

    _stub_entrypoint_deps(monkeypatch, run_build=slow_build)

    result = main.invoke({"id": "hybrid-x"}, types.SimpleNamespace(session_id="s" * 33))

    assert result["status"] == "processing"
    assert result["id"] == "hybrid-x"
    assert "task_id" in result, "must register an async task or the runtime goes idle"
    assert started.wait(timeout=5), "build never started on the background thread"
    assert not finished.is_set(), "invoke blocked until the build completed"
    release.set()
    assert finished.wait(timeout=5)


def test_invoke_background_failure_does_not_escape(monkeypatch):
    """A failing build must not raise out of the thread — status is already recorded."""
    done = threading.Event()

    def boom(**kwargs):
        try:
            raise RuntimeError("vkg down")
        finally:
            done.set()

    _stub_entrypoint_deps(monkeypatch, run_build=boom)

    result = main.invoke({"id": "hybrid-x"}, types.SimpleNamespace(session_id="s" * 33))

    assert result["status"] == "processing"
    assert done.wait(timeout=5)
