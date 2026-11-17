# tests/unit/test_ontology_delta_assembly.py
def test_ontology_filter_delta_targets():
    import agents.ontology_agent.main as o
    tables = [
        {"database": "db", "table": "a", "catalogId": "AWSDataCatalog"},
        {"database": "db", "table": "b", "catalogId": "AWSDataCatalog"},
    ]
    got = o._filter_delta_targets(tables, ["db.b"])
    assert [t["table"] for t in got] == ["b"]


def test_ontology_filter_none_returns_all():
    import agents.ontology_agent.main as o
    tables = [{"database": "db", "table": "a"}]
    assert o._filter_delta_targets(tables, None) == tables


from unittest.mock import patch
import json as _json


def test_delta_assembly_includes_all_tables_not_just_rebuilt():
    import agents.ontology_agent.main as o
    # Full set of 3 tables; imagine only 'b' was rebuilt this run.
    fragments = {"tables": [{"table_name": t, "fk_hints": ""} for t in ("a", "b", "c")]}

    def fake_read(oid, table):
        return _json.dumps({"success": True, "nquad_content": f"<{table}> a <T> ."})

    # Patch the S3 hydration to a no-op here; this test drives assembly off the
    # (patched) load_phase1_fragments set. The end-to-end truncation guard below
    # exercises the real hydration path.
    with patch.object(o, "_hydrate_missing_fragments_from_s3", return_value=None), \
         patch.object(o, "load_phase1_fragments", return_value=_json.dumps(fragments)), \
         patch.object(o, "read_local_nquads_file", side_effect=fake_read):
        consolidated = o._assemble_consolidated("L1")
    assert "<a>" in consolidated and "<b>" in consolidated and "<c>" in consolidated


def test_delta_assembly_hydrates_unchanged_fragments_from_s3(tmp_path, monkeypatch):
    """I3 end-to-end: on a delta build only the rebuilt table has a LOCAL fragment;
    unchanged tables live only in S3. load_phase1_fragments is local-first and would
    truncate to just the rebuilt table — assembly must first hydrate the missing
    fragments from S3 so the consolidated ontology.nq stays COMPLETE."""
    import tempfile
    import boto3
    from moto import mock_aws
    import agents.ontology_agent.main as o

    # Point tempfile at an isolated dir so the local phase1 set is controlled.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    local_dir = tmp_path / "ontologies" / "L1" / "phase1"
    local_dir.mkdir(parents=True)

    def _fragment_md(table, nquads):
        return f"**Table:** {table}\n\n```nquads\n{nquads}\n```\n"

    # Only 'b' was rebuilt this run → only b has a local fragment.
    (local_dir / "table-02-b.md").write_text(_fragment_md("b", "<b> a <T> ."))

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="artifacts")
        # S3 holds the full prior build: a, b, c.
        for step, t in ((1, "a"), (2, "b"), (3, "c")):
            s3.put_object(
                Bucket="artifacts",
                Key=f"ontologies/L1/phase1/table-0{step}-{t}.md",
                Body=_fragment_md(t, f"<{t}> a <T> .").encode("utf-8"),
            )
        monkeypatch.setenv("ARTIFACTS_BUCKET", "artifacts")
        # Session injection moved to the shared helper (agents.shared.boto_session);
        # patch the module-level get_boto_session name the agent calls.
        _session = boto3.Session(region_name="us-east-1")
        monkeypatch.setattr(o, "get_boto_session", lambda: _session)

        consolidated = o._assemble_consolidated("L1")

    # All three tables present — unchanged a and c were hydrated from S3.
    assert "<a>" in consolidated
    assert "<b>" in consolidated
    assert "<c>" in consolidated


def test_delta_assembly_no_stale_duplicate_when_step_number_changed(tmp_path, monkeypatch):
    """Regression for the hydration step-index divergence: a delta build renumbers
    the rebuilt table's fragment (fresh local `table-01-orders.md`) while S3 still
    holds the prior full-build copy under a DIFFERENT step (`table-05-orders.md`).
    Hydration must skip the stale S3 copy by TABLE NAME (not filename) so `orders`
    is not assembled twice, and the FRESH local content wins."""
    import tempfile
    import boto3
    from moto import mock_aws
    import agents.ontology_agent.main as o

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    local_dir = tmp_path / "ontologies" / "L1" / "phase1"
    local_dir.mkdir(parents=True)

    def _fragment_md(table, nquads):
        return f"**Table:** {table}\n\n```nquads\n{nquads}\n```\n"

    # Delta rebuild of 'orders' this run → fresh local fragment at a NEW step (01),
    # with NEW content.
    (local_dir / "table-01-orders.md").write_text(
        _fragment_md("orders", "<orders_fresh> a <T> .")
    )

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="artifacts")
        # Prior full build in S3: orders at step 05 (STALE content) + an unchanged
        # sibling 'items' at step 06.
        s3.put_object(
            Bucket="artifacts", Key="ontologies/L1/phase1/table-05-orders.md",
            Body=_fragment_md("orders", "<orders_STALE> a <T> .").encode("utf-8"),
        )
        s3.put_object(
            Bucket="artifacts", Key="ontologies/L1/phase1/table-06-items.md",
            Body=_fragment_md("items", "<items> a <T> .").encode("utf-8"),
        )
        monkeypatch.setenv("ARTIFACTS_BUCKET", "artifacts")
        # Session injection moved to the shared helper (agents.shared.boto_session);
        # patch the module-level get_boto_session name the agent calls.
        _session = boto3.Session(region_name="us-east-1")
        monkeypatch.setattr(o, "get_boto_session", lambda: _session)

        consolidated = o._assemble_consolidated("L1")

    # Fresh orders content wins; stale copy never pulled in; items hydrated.
    assert "<orders_fresh>" in consolidated
    assert "<orders_STALE>" not in consolidated
    assert "<items>" in consolidated
    # 'orders' appears exactly once (no stale duplicate).
    assert consolidated.count("<orders_fresh>") == 1


def test_ontology_records_fingerprint_after_phase2():
    import agents.ontology_agent.main as o
    from unittest.mock import MagicMock, patch
    with patch.object(o, "get_single_table_schema",
                      return_value='{"columns":[{"name":"id","type":"string"}]}'), \
         patch.object(o, "_data_marker_for_table", return_value=("snap9", "iceberg_snapshot")), \
         patch.object(o, "write_table_fingerprint") as wtf:  # bound into agent module
        o._record_table_fingerprint(MagicMock(), "L1", "v3", "db", "holding", "s3tablescatalog/x")
    assert wtf.call_args.kwargs["db_table"] == "db.holding"
    assert wtf.call_args.kwargs["fp"]["markerKind"] == "iceberg_snapshot"
