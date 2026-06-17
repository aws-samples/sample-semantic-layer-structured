"""Unit tests for the ungated ``_run_athena_sql`` helper in the VKG
ontology_query_agent.

``_run_athena_sql`` is the deterministic Athena-execution core that Phase 5
calls directly. It returns a structured dict (incl. ``state_change_reason``) and
does NOT raise on FAILED/CANCELLED so Phase 5 can inspect the failure and run LLM
SQL-repair.

Note: the legacy ``execute_sql_query`` ``@tool`` wrapper (gate on
``disambiguation_complete``, set ``query_executed``, return a compact summary
string) was REMOVED when the query agents became graph-only — the single-shot
ReAct ``@tool`` functions no longer exist. Tests for that removed contract were
deleted; ``_run_athena_sql`` is now the sole execution entry point here.
"""
from agents.ontology_query_agent import main


# ---------------------------------------------------------------------------
# Fake Athena / SSM / S3 boto3 surface
# ---------------------------------------------------------------------------


class _FakeAthena:
    """Build a fake boto3 session whose clients mimic the exact calls
    ``_run_athena_sql`` makes:

      * athena.start_query_execution(...) -> {'QueryExecutionId': 'q1'}
      * athena.get_query_execution(...)   -> status (SUCCEEDED / FAILED / ...)
      * athena.get_paginator('get_query_results') -> one page: header + rows
      * ssm.get_parameter(...)            -> {'Parameter': {'Value': bucket}}
      * s3.put_object(...)                -> no-op (result-set offload)
    """

    def __init__(self, *, state: str, columns: list, rows: list,
                 reason: str = "", qid: str = "q1",
                 bucket: str = "my-athena-bucket") -> None:
        self.state = state
        self.columns = columns
        self.rows = rows
        self.reason = reason
        self.qid = qid
        self.bucket = bucket
        # Spies for assertions
        self.started_with: dict | None = None

    # -- athena client ------------------------------------------------------
    def _athena_client(self):
        fake = self

        class _Paginator:
            def paginate(self, **_kwargs):
                # One page: header row of column names, then data rows.
                def _row(values):
                    return {"Data": [{"VarCharValue": v} for v in values]}

                page = {
                    "ResultSet": {
                        "Rows": [_row(fake.columns)] + [_row(r) for r in fake.rows]
                    }
                }
                return [page]

        class _Athena:
            def start_query_execution(self, **kwargs):
                fake.started_with = kwargs
                return {"QueryExecutionId": fake.qid}

            def get_query_execution(self, **_kwargs):
                status = {"State": fake.state}
                if fake.state in ("FAILED", "CANCELLED") and fake.reason:
                    status["StateChangeReason"] = fake.reason
                return {"QueryExecution": {"Status": status, "Statistics": {}}}

            def get_paginator(self, name):
                assert name == "get_query_results"
                return _Paginator()

        return _Athena()

    # -- ssm client ---------------------------------------------------------
    def _ssm_client(self):
        fake = self

        class _Ssm:
            def get_parameter(self, **_kwargs):
                return {"Parameter": {"Value": fake.bucket}}

        return _Ssm()

    # -- s3 client ----------------------------------------------------------
    def _s3_client(self):
        class _S3:
            def put_object(self, **_kwargs):
                return {}

        return _S3()

    # -- session ------------------------------------------------------------
    def session(self):
        fake = self

        class _Session:
            region_name = "us-east-1"

            def client(self, service_name, **_kwargs):
                if service_name == "athena":
                    return fake._athena_client()
                if service_name == "ssm":
                    return fake._ssm_client()
                if service_name == "s3":
                    return fake._s3_client()
                raise AssertionError(f"unexpected client: {service_name}")

        return _Session()


# ---------------------------------------------------------------------------
# _run_athena_sql — happy path
# ---------------------------------------------------------------------------


def test_run_athena_sql_shapes_columns_rows(monkeypatch):
    fake = _FakeAthena(state="SUCCEEDED", columns=["n"], rows=[["10"]])
    monkeypatch.setattr(main, "get_boto_session", lambda: fake.session())
    out = main._run_athena_sql(sql="SELECT COUNT(*) n FROM normalized.admin_codes",
                               database_name="normalized", catalog_id="AwsDataCatalog")
    assert out["columns"] == ["n"]
    assert out["rows"] == [["10"]]
    assert out.get("state_change_reason") in (None, "")
    assert out["query_execution_id"] == "q1"
    assert out["over_limit"] is False


def test_run_athena_sql_no_gate(monkeypatch):
    """The helper must run regardless of disambiguation_complete (ungated)."""
    main._agent_state["disambiguation_complete"] = False
    fake = _FakeAthena(state="SUCCEEDED", columns=["n"], rows=[["10"]])
    monkeypatch.setattr(main, "get_boto_session", lambda: fake.session())
    out = main._run_athena_sql(sql="SELECT 1 n", database_name="db",
                               catalog_id="AwsDataCatalog")
    assert out["rows"] == [["10"]]


def test_run_athena_sql_standard_glue_no_catalog_in_context(monkeypatch):
    fake = _FakeAthena(state="SUCCEEDED", columns=["n"], rows=[["1"]])
    monkeypatch.setattr(main, "get_boto_session", lambda: fake.session())
    main._run_athena_sql(sql="SELECT 1 n", database_name="db",
                         catalog_id="AwsDataCatalog")
    ctx = fake.started_with["QueryExecutionContext"]
    assert "Catalog" not in ctx
    assert ctx["Database"] == "db"


def test_run_athena_sql_s3tables_catalog_in_context(monkeypatch):
    fake = _FakeAthena(state="SUCCEEDED", columns=["n"], rows=[["1"]])
    monkeypatch.setattr(main, "get_boto_session", lambda: fake.session())
    main._run_athena_sql(sql="SELECT 1 n", database_name="db",
                         catalog_id="s3tablescatalog/my-bucket")
    ctx = fake.started_with["QueryExecutionContext"]
    assert ctx["Catalog"] == "s3tablescatalog/my-bucket"
    assert ctx["Database"] == "db"


def test_run_athena_sql_failed_returns_reason_no_raise(monkeypatch):
    """FAILED -> dict with state_change_reason, rows=[], columns=[]; no raise."""
    fake = _FakeAthena(state="FAILED", columns=[], rows=[],
                       reason="SYNTAX_ERROR: line 1:1 near 'FRM'")
    monkeypatch.setattr(main, "get_boto_session", lambda: fake.session())
    out = main._run_athena_sql(sql="SELECT 1 FRM x", database_name="db",
                               catalog_id="AwsDataCatalog")
    assert out["columns"] == []
    assert out["rows"] == []
    assert "SYNTAX_ERROR" in out["state_change_reason"]
    assert out["query_execution_id"] == "q1"
