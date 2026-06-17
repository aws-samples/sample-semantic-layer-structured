"""
Integration Tests for Virtual KG Query Agent
Tests with real AWS infrastructure (Athena).

The deployed VKG agent is a deterministic Tier 2 Strands graph: Phase 5
translates the grounded SPARQL to SQL (Ontop) and runs it on Athena via
``_run_athena_sql`` — there is no model tool loop, and the legacy
disambiguate_query_terms / execute_sql_query / map_sql_results_to_rdf @tools and
create_query_agent factory have been removed. This suite therefore exercises the
surviving deterministic Athena-execution core (``_run_athena_sql``) against a
real table.

NOTE: get_ontology_from_neptune and execute_sparql_query are MCP Gateway tools
accessible only through the running AgentCore Gateway — they cannot be imported
or called directly in integration tests.

Required Environment Variables:
- AWS_REGION (default: us-east-1)
- TEST_DATABASE (default: default)
- TEST_TABLE: Table name for Athena query tests (Athena tests skip if unset)
- TEST_CATALOG (default: AwsDataCatalog)
- ATHENA_RESULTS_BUCKET (or SSM parameter /<project>/athena/query-results-bucket)
"""

import sys
import os

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from ontology_query_agent.main import _run_athena_sql

# Test configuration
TEST_DATABASE = os.getenv('TEST_DATABASE', 'default')
TEST_TABLE = os.getenv('TEST_TABLE', '')
TEST_CATALOG = os.getenv('TEST_CATALOG', 'AwsDataCatalog')


def test_1_athena_query():
    """Test 1: Execute a SQL query against a real Athena table via _run_athena_sql."""
    print("\n=== Integration Test 1: Athena Query Execution ===")
    if not TEST_TABLE:
        print("⚠️  TEST_TABLE not set — skipping (set TEST_TABLE=<your_table_name>)")
        return True

    try:
        sql_query = f'SELECT * FROM "{TEST_TABLE}" LIMIT 5'  # nosec B608 — test-only SQL; TEST_TABLE is a test constant, not user input
        print(f"Test SQL: {sql_query}")

        result = _run_athena_sql(
            sql=sql_query, database_name=TEST_DATABASE, catalog_id=TEST_CATALOG,
        )

        # _run_athena_sql converts a *query* failure to a dict with
        # state_change_reason set (it does not raise); only infra errors raise.
        reason = result.get('state_change_reason')
        if reason:
            print(f"❌ Athena query failed: {reason}")
            print("Check: database/table exist, Athena permissions, ATHENA_RESULTS_BUCKET")
            return False

        columns = result.get('columns', [])
        rows = result.get('rows', [])
        print(f"✅ Query executed successfully")
        print(f"✅ Query ID: {result.get('query_execution_id')}")
        print(f"✅ Columns: {len(columns)} | Rows: {len(rows)}")
        if columns:
            print(f"   Columns: {', '.join(columns[:5])}")

        return True

    except Exception as e:
        print(f"❌ Athena query test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def run_all_integration_tests():
    """Run all integration tests."""
    print("=" * 70)
    print("Virtual KG Query Agent - Integration Test Suite")
    print("=" * 70)
    print(f"\nTest Database : {TEST_DATABASE}")
    print(f"Test Table    : {TEST_TABLE or '(not set — Athena tests will skip)'}")
    print(f"Test Catalog  : {TEST_CATALOG}")
    print(f"AWS Region    : {os.getenv('AWS_REGION', 'us-east-1')}")
    print("\nNote: Neptune/Gateway + the deterministic graph phases are covered")
    print("      elsewhere; this suite exercises the real Athena execution core.")
    print("=" * 70)

    tests = [
        test_1_athena_query,
    ]

    results = []
    for test in tests:
        try:
            results.append(test())
        except Exception as e:
            print(f"❌ Test crashed: {str(e)}")
            results.append(False)

    print("\n" + "=" * 70)
    print("Integration Test Summary")
    print("=" * 70)
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    print(f"Failed: {total - passed}/{total}")

    if passed == total:
        print("\n✅ All integration tests passed!")
        return 0
    print("\n⚠️  Some integration tests failed (require real AWS infrastructure)")
    return 1


if __name__ == "__main__":
    sys.exit(run_all_integration_tests())
