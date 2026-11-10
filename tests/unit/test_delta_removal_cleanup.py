"""
Unit test for tool_delete_table_triples (per-table triple delete).

The removal-cleanup path (Task 12) drops a removed table's triples without
touching sibling tables. This test asserts the tool builds a SPARQL update
scoped to the layer's named graph and the real vkg:mapsToTable "db.table"
literal, then routes it through the shared _sparql_update write path.
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch

# Load lambda/neptune-tools/index.py by file path under a UNIQUE module name.
# Several Lambda handlers under test are all named `index.py`; a plain
# `import index` caches the first one in sys.modules['index'] and hands later
# test files the WRONG module under the full-suite run. A distinct name avoids
# that cross-test collision.
_NEPTUNE_TOOLS_INDEX_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "lambda", "neptune-tools", "index.py"
    )
)
_spec = importlib.util.spec_from_file_location(
    "neptune_tools_index_delta", _NEPTUNE_TOOLS_INDEX_PATH
)
index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index)

# Make `services.*` importable so the planner cleanup tests below can import
# DeltaPlannerService. This is separate from the neptune importlib loader above,
# which stays intact for the Task 11 test.
import sys  # noqa: E402

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "lambda", "rest-api")
)


def test_delete_table_triples_builds_scoped_sparql():
    """The tool builds a graph- and table-scoped DELETE and reports success."""
    resp = MagicMock()
    resp.status_code = 200
    with (
        patch.object(index, "_sparql_update", return_value=resp) as upd,
        patch.object(index, "_resolve_graph_uri", return_value="http://g/L1"),
        patch.object(index, "_validate_ontology_id", side_effect=lambda x: x),
    ):
        out = index.tool_delete_table_triples(
            ontology_id="L1", database_name="insurance_db", table_name="customer"
        )
    sparql = upd.call_args.args[0]
    assert "http://g/L1" in sparql  # layer graph
    assert '"insurance_db.customer"' in sparql  # real literal
    assert "https://semantic-layer.aws/virtual-kg/mapsToTable" in sparql  # real IRI
    assert "DELETE" in sparql.upper()
    assert '"success": true' in out.lower()


def test_delete_table_triples_rejects_injection_table_name():
    """C2: a table name containing a double-quote must be rejected BEFORE any
    SPARQL is built/sent — otherwise it breaks out of the "<db>.<table>" literal
    and could rewrite the WHERE clause to delete the entire layer graph."""
    with (
        patch.object(index, "_sparql_update") as upd,
        patch.object(index, "_resolve_graph_uri", return_value="http://g/L1"),
        patch.object(index, "_validate_ontology_id", side_effect=lambda x: x),
    ):
        out = index.tool_delete_table_triples(
            ontology_id="L1",
            database_name="db",
            table_name='x" . } DELETE { ?s ?p ?o } WHERE { ?s ?p ?o } #',
        )
    upd.assert_not_called()  # never reaches the write path
    assert '"success": false' in out.lower()


def test_delete_table_triples_rejects_injection_database_name():
    """C2 companion: the database half is validated too."""
    with (
        patch.object(index, "_sparql_update") as upd,
        patch.object(index, "_resolve_graph_uri", return_value="http://g/L1"),
        patch.object(index, "_validate_ontology_id", side_effect=lambda x: x),
    ):
        out = index.tool_delete_table_triples(
            ontology_id="L1", database_name='db" .}', table_name="customer"
        )
    upd.assert_not_called()
    assert '"success": false' in out.lower()


def test_delete_table_triples_accepts_hyphenated_ddb_table_name():
    """C2 regression guard: the injection validation must NOT reject legitimate
    hyphenated DynamoDB-connector table names (e.g. semantic-layer-admin-codes) —
    hyphens/dots are harmless inside the double-quoted SPARQL literal. An over-tight
    allowlist previously silently left such tables' triples in Neptune on removal."""
    resp = MagicMock()
    resp.status_code = 200
    with (
        patch.object(index, "_sparql_update", return_value=resp) as upd,
        patch.object(index, "_resolve_graph_uri", return_value="http://g/L1"),
        patch.object(index, "_validate_ontology_id", side_effect=lambda x: x),
    ):
        out = index.tool_delete_table_triples(
            ontology_id="L1",
            database_name="default",
            table_name="semantic-layer-admin-codes",
        )
    upd.assert_called_once()  # reaches the write path
    sparql = upd.call_args.args[0]
    assert '"default.semantic-layer-admin-codes"' in sparql  # literal intact
    assert '"success": true' in out.lower()


# --- Task 12: DeltaPlannerService.cleanup_removed -------------------------------

from services.delta_planner_service import DeltaPlannerService  # noqa: E402


def test_cleanup_removed_deletes_rag_s3_docs():
    svc = DeltaPlannerService(
        metadata_table=MagicMock(),
        glue_service=MagicMock(),
        artifacts_bucket="b",
        region="us-east-1",
    )
    with patch("boto3.client") as bc:
        s3 = MagicMock()
        bc.return_value = s3
        svc.cleanup_removed(
            "L1",
            version="v3",
            layer_type="SemanticRAG",
            removed=[("db", "gone", "AWSDataCatalog")],
        )
    deleted = [c.kwargs["Key"] for c in s3.delete_object.call_args_list]
    assert any(k.endswith("gone.md") for k in deleted)
    assert any(k.endswith("gone.md.metadata.json") for k in deleted)


def test_cleanup_removed_vkg_calls_neptune_and_deletes_fragment():
    svc = DeltaPlannerService(
        metadata_table=MagicMock(),
        glue_service=MagicMock(),
        artifacts_bucket="b",
        region="us-east-1",
    )
    with (
        patch("boto3.client"),
        patch.object(svc, "_neptune_delete_table") as nd,
        patch.object(svc, "_delete_phase1_fragment") as df,
    ):
        svc.cleanup_removed(
            "L1",
            version="v3",
            layer_type="VKG",
            removed=[("db", "gone", "s3tablescatalog/x")],
        )
    # Neptune delete AND fragment delete need the DATABASE too (mapsToTable literal
    # is "db.table"; fragment delete disambiguates same-named siblings by it).
    nd.assert_called_once_with("L1", "db", "gone")
    df.assert_called_once_with("L1", "db", "gone")


def test_delete_phase1_fragment_spares_same_named_sibling_in_other_db():
    """I1: two databases in one layer both have a table `orders`
    (table-03-orders.md / table-07-orders.md). Removing db1.orders must delete ONLY
    db1's fragment — matched on the mapsToTable "db.table" literal in the body — and
    leave db2.orders' fragment intact."""
    from services.delta_planner_service import DeltaPlannerService

    svc = DeltaPlannerService(
        metadata_table=MagicMock(),
        glue_service=MagicMock(),
        artifacts_bucket="b",
        region="us-east-1",
    )
    PRED = "https://semantic-layer.aws/virtual-kg/mapsToTable"
    bodies = {
        "ontologies/L1/phase1/table-03-orders.md":
            f'<c1> <{PRED}> "db1.orders" <g> .',
        "ontologies/L1/phase1/table-07-orders.md":
            f'<c2> <{PRED}> "db2.orders" <g> .',
    }

    class _Body:
        def __init__(self, text):
            self._t = text.encode("utf-8")

        def read(self):
            return self._t

    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": k} for k in bodies]}
    ]
    s3.get_object.side_effect = lambda Bucket, Key: {"Body": _Body(bodies[Key])}

    with patch("boto3.client", return_value=s3):
        svc._delete_phase1_fragment("L1", "db1", "orders")

    deleted = [c.kwargs["Key"] for c in s3.delete_object.call_args_list]
    assert deleted == ["ontologies/L1/phase1/table-03-orders.md"]  # db1 only
    assert "ontologies/L1/phase1/table-07-orders.md" not in deleted  # db2 spared


def test_cleanup_removed_rag_defaults_catalog_when_no_fingerprint():
    # M-3: a table removed with no stored fingerprint defaults to AWSDataCatalog.
    # cleanup_removed itself takes explicit tuples, so the DEFAULTING is exercised
    # in the service layer (Task 19). Here we assert cleanup still runs with the
    # defaulted catalog and does not raise.
    svc = DeltaPlannerService(
        metadata_table=MagicMock(),
        glue_service=MagicMock(),
        artifacts_bucket="b",
        region="us-east-1",
    )
    with patch("boto3.client") as bc:
        s3 = MagicMock()
        bc.return_value = s3
        svc.cleanup_removed(
            "L1",
            version="v3",
            layer_type="SemanticRAG",
            removed=[("db", "gone", "AWSDataCatalog")],
        )
    keys = [c.kwargs["Key"] for c in s3.delete_object.call_args_list]
    assert any("AWSDataCatalog" in k and k.endswith("gone.md") for k in keys)
