"""Progress counters for the Hybrid two-phase build.

``ProgressWriter`` used to persist ONLY ``status``. The build page reads
``totalTables`` / ``tablesProcessed`` / ``currentTable`` / ``progressPercent``
from the same record, so with those attributes never written the API had nothing
to forward and the completion screen reported "0 tables" for a layer whose graph
had been built correctly.

These tests pin the counter writes, the reserved-word aliasing that makes them
safe, the two-phase percentage split, and the removal of a previous attempt's
failure fields on a successful rebuild.
"""
from agents.semantic_layer_creation_agent import main
from agents.semantic_layer_creation_agent.progress import ProgressWriter


class _FakeTable:
    """Record every ``update_item`` call instead of talking to DynamoDB."""

    def __init__(self):
        """Start with an empty call log."""
        self.calls = []

    def update_item(self, **kwargs):
        """Append the update kwargs to the call log."""
        self.calls.append(kwargs)


def _writer():
    """Build a ProgressWriter bound to a fake table.

    Returns:
        tuple[ProgressWriter, _FakeTable]: the writer and the table it writes to.
    """
    table = _FakeTable()
    return (
        ProgressWriter(job_id="lay", version="v1", table_factory=lambda name: table),
        table,
    )


def test_status_only_write_still_works():
    writer, table = _writer()
    writer.write("buildingKB")
    assert table.calls[0]["ExpressionAttributeValues"][":status"] == "buildingKB"


def test_counter_fields_are_persisted():
    writer, table = _writer()
    writer.write(
        "buildingKB",
        fields={"totalTables": 8, "tablesProcessed": 3, "currentTable": "cards",
                "progressPercent": 18},
    )
    call = table.calls[0]
    names = call["ExpressionAttributeNames"]
    values = call["ExpressionAttributeValues"]
    written = {names[alias]: values[f":v{alias[2:]}"]
               for alias in names if alias.startswith("#k")}
    assert written == {"totalTables": 8, "tablesProcessed": 3,
                       "currentTable": "cards", "progressPercent": 18}


def test_every_field_name_is_aliased():
    """`status` and friends are DynamoDB reserved words; an unaliased name fails."""
    writer, table = _writer()
    writer.write("buildingVKG", fields={"status": "x", "percent": 1})
    expression = table.calls[0]["UpdateExpression"]
    assert "status =" not in expression.replace("#status =", "")
    assert "percent" not in expression        # only aliases appear


def test_remove_clears_previous_failure_fields():
    writer, table = _writer()
    writer.write("completed", remove=["error", "failedAt"])
    call = table.calls[0]
    assert "REMOVE" in call["UpdateExpression"]
    removed = {call["ExpressionAttributeNames"][alias]
               for alias in call["ExpressionAttributeNames"]
               if alias.startswith("#r")}
    assert removed == {"error", "failedAt"}


def test_a_dynamodb_error_never_crashes_the_build_thread():
    class _Boom:
        def update_item(self, **kwargs):
            raise RuntimeError("throttled")

    writer = ProgressWriter(job_id="lay", version="v1",
                            table_factory=lambda name: _Boom())
    writer.write("buildingKB")      # must not raise


# --- _run_build: the two-phase percentage split -------------------------------
def _capture_run_build(monkeypatch):
    """Stub _run_build's seams and capture every status/field write.

    Args:
        monkeypatch: pytest's monkeypatch fixture.

    Returns:
        tuple[list, list]: progress writes as ``(status, fields)`` and terminal
            writes as the kwargs dicts passed to ``_write_terminal_status``.
    """
    writes = []
    terminal = []

    class _Recorder:
        def __init__(self, **kwargs):
            """Ignore the binding args; record writes only."""

        def write(self, status, *, fields=None, remove=None):
            """Record one progress write."""
            writes.append((status, fields, remove))

    monkeypatch.setattr(
        "agents.semantic_layer_creation_agent.progress.ProgressWriter", _Recorder
    )
    monkeypatch.setattr(
        main, "_write_terminal_status", lambda **kwargs: terminal.append(kwargs)
    )
    return writes, terminal


def test_run_build_seeds_the_table_total_before_the_first_phase(monkeypatch):
    """The page polls immediately; a record without totalTables renders "0 tables"."""
    writes, _terminal = _capture_run_build(monkeypatch)
    monkeypatch.setattr(main, "_make_build_kb", lambda **kwargs: lambda: None)
    monkeypatch.setattr(main, "_make_build_vkg", lambda **kwargs: lambda: None)

    main._run_build(id="lay", version="v1", config={},
                    tables=[{"table": "a"}, {"table": "b"}])
    assert writes[0][1]["totalTables"] == 2


def test_the_two_phases_share_one_monotonic_percentage(monkeypatch):
    """KB fills the first band, VKG the next — the bar must never go backwards."""
    writes, _terminal = _capture_run_build(monkeypatch)
    reporters = {}

    def capture_kb(**kwargs):
        reporters["kb"] = kwargs["report_progress"]
        return lambda: None

    def capture_vkg(**kwargs):
        reporters["vkg"] = kwargs["report_progress"]
        return lambda: None

    monkeypatch.setattr(main, "_make_build_kb", capture_kb)
    monkeypatch.setattr(main, "_make_build_vkg", capture_vkg)
    main._run_build(id="lay", version="v1", config={},
                    tables=[{"table": "a"}, {"table": "b"}])

    reporters["kb"](phase="kb", done=1, total=2, current="a")
    reporters["kb"](phase="kb", done=2, total=2, current="b")
    reporters["vkg"](phase="vkg", done=2, total=2, current="b")

    phase_writes = [(status, fields["progressPercent"])
                    for status, fields, _remove in writes if fields
                    and "progressPercent" in fields and "currentTable" in fields]
    assert [status for status, _pct in phase_writes] == [
        "buildingKB", "buildingKB", "buildingVKG"
    ]
    percents = [pct for _status, pct in phase_writes]
    assert percents == sorted(percents)
    assert percents[-1] < 100      # 100 belongs to the terminal write alone


def test_terminal_success_reports_full_counts_and_clears_stale_failure(monkeypatch):
    _writes, terminal = _capture_run_build(monkeypatch)
    monkeypatch.setattr(main, "_make_build_kb", lambda **kwargs: lambda: None)
    monkeypatch.setattr(main, "_make_build_vkg", lambda **kwargs: lambda: None)

    main._run_build(id="lay", version="v1", config={},
                    tables=[{"table": "a"}, {"table": "b"}])
    final = terminal[-1]
    assert final["status"] == "completed"
    assert final["totalTables"] == 2 and final["tablesProcessed"] == 2
    assert final["progressPercent"] == 100
    # A rebuild that succeeds must not keep showing the old attempt's error.
    assert set(final["remove"]) == {"error", "failedAt"}
