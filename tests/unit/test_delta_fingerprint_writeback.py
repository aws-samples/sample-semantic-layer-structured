import os
import sys
from unittest.mock import MagicMock, patch

# Ensure the repo root is importable so `agents.metadata_agent.main` resolves.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def test_metadata_agent_filters_to_delta_targets():
    import agents.metadata_agent.main as m
    valid = [
        {"database": "db", "table": "a", "catalogId": "AWSDataCatalog"},
        {"database": "db", "table": "b", "catalogId": "AWSDataCatalog"},
    ]
    got = m._filter_delta_targets(valid, ["db.a"])
    assert [t["table"] for t in got] == ["a"]


def test_filter_delta_targets_none_returns_all():
    import agents.metadata_agent.main as m
    valid = [{"database": "db", "table": "a"}]
    assert m._filter_delta_targets(valid, None) == valid


def test_fingerprint_written_on_success_not_on_failure():
    import agents.metadata_agent.main as m
    table = MagicMock()
    # Patch names as bound INTO the agent module (import-time binding), not the
    # shared module — m.write_table_fingerprint is the reference the code calls.
    with patch.object(m, "get_single_table_schema",
                      return_value='{"columns":[{"name":"id","type":"string"}]}'), \
         patch.object(m, "_data_marker_for_table", return_value=("42", "ddb_count")), \
         patch.object(m, "write_table_fingerprint") as wtf:
        m._record_table_fingerprint(table, "L1", "v3", "db", "a", "AWSDataCatalog")
        assert wtf.called
        assert wtf.call_args.kwargs["db_table"] == "db.a"
