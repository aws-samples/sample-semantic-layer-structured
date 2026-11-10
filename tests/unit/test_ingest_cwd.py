"""Unit tests for the CWD DDL → column-schema parser (sqlglot-based) and the
CWD benchmark-question extractor (rdflib-based)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.benchmarks.ingest_cwd import extract_cwd_questions, parse_ddl_tables

# A realistic slice of the REAL data.world CWD ACME_Insurance DDL (T-SQL dialect):
# no semicolons between statements (blank-line separated), PRIMARY KEY (Col ASC)
# constraints, and FOREIGN KEY constraint lines — all of which break a generic
# sqlglot.parse of the whole file. parse_ddl_tables must split into per-table
# blocks, strip ASC/DESC, and parse each with read="tsql".
_TSQL_DDL = """
CREATE TABLE Claim
(
    Claim_Identifier     int  NOT NULL ,
    Claim_Description    varchar(5000)  NULL ,
    Claims_Made_Date     datetime  NULL ,
    Claim_Amount         decimal(15,2)  NULL ,
    Status_Code          char(1)  NULL ,
     PRIMARY KEY (Claim_Identifier ASC),
     FOREIGN KEY (Claim_Identifier) REFERENCES Claim(Claim_Identifier)
)

CREATE TABLE Party
(
    Party_Identifier     bigint  NOT NULL ,
    Party_Name           varchar(100)  NULL ,
     PRIMARY KEY (Party_Identifier ASC)
)
"""


def test_parse_tsql_ddl_multi_table_no_semicolons():
    # The real CWD DDL has NO semicolons between CREATE TABLE statements; the
    # parser must split blocks itself and parse both tables.
    tables = parse_ddl_tables(ddl=_TSQL_DDL)
    assert set(tables) == {"Claim", "Party"}
    # PRIMARY KEY / FOREIGN KEY lines are constraints, not ColumnDefs → excluded.
    assert [c[0] for c in tables["Claim"]] == [
        "Claim_Identifier",
        "Claim_Description",
        "Claims_Made_Date",
        "Claim_Amount",
        "Status_Code",
    ]
    assert [c[0] for c in tables["Party"]] == ["Party_Identifier", "Party_Name"]


def test_parse_tsql_datetime_and_decimal_map_to_varchar():
    # datetime/decimal/char are stored as UTF-8 strings in Parquet, so their
    # DECLARED Athena type must be varchar (else HIVE_BAD_DATA on CTAS read).
    # int → bigint; bigint → bigint.
    types = dict(parse_ddl_tables(ddl=_TSQL_DDL)["Claim"])
    assert types["Claims_Made_Date"] == "varchar"  # datetime
    assert types["Claim_Amount"] == "varchar"  # decimal(15,2)
    assert types["Status_Code"] == "varchar"  # char(1)
    assert types["Claim_Identifier"] == "bigint"  # int
    party = dict(parse_ddl_tables(ddl=_TSQL_DDL)["Party"])
    assert party["Party_Identifier"] == "bigint"  # bigint


def test_parse_emits_only_physical_types():
    # Every emitted type across the whole DDL must be one of the three physical
    # forms csv_to_parquet writes: {bigint, double, varchar}.
    tables = parse_ddl_tables(ddl=_TSQL_DDL)
    emitted = {t for cols in tables.values() for _, t in cols}
    assert emitted <= {"bigint", "double", "varchar"}, emitted


def test_parse_ddl_single_table_columns_and_types():
    # Generic ANSI-style DDL (with semicolons, PRIMARY KEY line) must still parse
    # under the T-SQL reader and honor the physical-type invariant.
    ddl = """
    CREATE TABLE policy (
        policy_id   VARCHAR(20) NOT NULL,
        premium     DECIMAL(10,2),
        issued_on   DATE,
        PRIMARY KEY (policy_id)
    );
    """
    tables = parse_ddl_tables(ddl=ddl)
    cols = tables["policy"]
    names = [c[0] for c in cols]
    assert names == ["policy_id", "premium", "issued_on"]  # PK line excluded
    types = dict(cols)
    # CWD stores every non-numeric/decimal column as a UTF-8 string in Parquet, so
    # the DECLARED Athena type must be varchar to match the physical type (else
    # Athena CTAS raises HIVE_BAD_DATA). Numeric int/float columns keep bigint/double.
    assert types["premium"] == "varchar"
    assert types["issued_on"] == "varchar"
    assert types["policy_id"] == "varchar"  # length dropped (Iceberg has none)


def test_parse_ddl_multiple_tables():
    ddl = """
    CREATE TABLE a (id INT);
    CREATE TABLE b (name VARCHAR(10), amt NUMERIC(5,2));
    """
    tables = parse_ddl_tables(ddl=ddl)
    assert set(tables) == {"a", "b"}
    assert dict(tables["a"])["id"] == "bigint"  # INT -> bigint (written int64)
    # decimal stored as string in Parquet, so declared varchar (HIVE_BAD_DATA else)
    assert dict(tables["b"])["amt"] == "varchar"


def test_parse_ddl_emits_only_physical_athena_types():
    # Every emitted type must be one of {bigint, double, varchar} so the declared
    # external-table type matches the physical Parquet type csv_to_parquet writes.
    ddl = """CREATE TABLE t (a INT, b BIGINT, c DOUBLE, d FLOAT, e DATE,
             f TIMESTAMP, g BOOLEAN, h DECIMAL(10,2), i VARCHAR(5), j TEXT);"""
    types = set(v for _, v in parse_ddl_tables(ddl=ddl)["t"])
    assert types <= {"bigint", "double", "varchar"}, types


# ---------------------------------------------------------------------------
# CWD benchmark-question extractor (rdflib) tests.
#
# CWD's NL-question + gold-SQL pairs live in a Turtle (.ttl) file, NOT JSON.
# Each dwt:SqlQuery node carries QandA:queryText (gold SQL) + dct:description
# (the NL question). dwt:SparqlQuery nodes MUST be skipped — we score SQL only.
# ---------------------------------------------------------------------------
_CWD_TTL = """
@prefix QandA: <http://models.data.world/benchmarks/QandA#> .
@prefix dct:   <http://purl.org/dc/terms/> .
@prefix dwt:   <https://templates.data.world/> .
@prefix rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

dwt:q1 rdf:type dwt:SqlQuery ; QandA:inLanguage QandA:SQL ;
    QandA:queryText "SELECT COUNT(*) FROM claim" ; dct:description "How many claims?" .
dwt:q2 rdf:type dwt:SqlQuery ; QandA:inLanguage QandA:SQL ;
    QandA:queryText "SELECT policy_number FROM policy" ; dct:description "List policy numbers." .
dwt:q3 rdf:type dwt:SparqlQuery ; QandA:inLanguage QandA:SPARQL ;
    QandA:queryText "SELECT ?p WHERE { ?p a in:Policy }" ; dct:description "SPARQL variant (skip)." .
"""

# A TTL that IS a benchmark file (uses the QandA namespace) but has ZERO
# dwt:SqlQuery nodes — only a SparqlQuery. Extraction must fail loud here.
_CWD_TTL_SPARQL_ONLY = """
@prefix QandA: <http://models.data.world/benchmarks/QandA#> .
@prefix dct:   <http://purl.org/dc/terms/> .
@prefix dwt:   <https://templates.data.world/> .
@prefix rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

dwt:q3 rdf:type dwt:SparqlQuery ; QandA:inLanguage QandA:SPARQL ;
    QandA:queryText "SELECT ?p WHERE { ?p a in:Policy }" ; dct:description "SPARQL only." .
"""


def test_extract_cwd_questions_sql_only(tmp_path: Path):
    # Two SqlQuery nodes + one SparqlQuery node; the SPARQL variant must be
    # skipped (we score SQL only) and the two SQL pairs mapped correctly.
    ttl = tmp_path / "acme-benchmark.ttl"
    ttl.write_text(_CWD_TTL, encoding="utf-8")

    rows = extract_cwd_questions(ttl_path=ttl, db_id="cwd_acme")

    assert len(rows) == 2  # SparqlQuery node skipped
    by_q = {r.question: r for r in rows}
    assert by_q["How many claims?"].gold_sql == "SELECT COUNT(*) FROM claim"
    assert by_q["List policy numbers."].gold_sql == "SELECT policy_number FROM policy"
    assert all(r.db_id == "cwd_acme" for r in rows)
    # No SPARQL leaked through.
    assert not any("SPARQL" in r.question for r in rows)


def test_extract_cwd_questions_empty_ttl_raises(tmp_path: Path):
    # A benchmark TTL (QandA namespace present) with ZERO SqlQuery nodes means
    # the format changed / we're pointed at the wrong file — fail loud rather
    # than silently emit an empty question set.
    ttl = tmp_path / "acme-benchmark.ttl"
    ttl.write_text(_CWD_TTL_SPARQL_ONLY, encoding="utf-8")

    with pytest.raises(ValueError, match="ZERO .*SqlQuery"):
        extract_cwd_questions(ttl_path=ttl, db_id="cwd_acme")
