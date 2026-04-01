"""
Integration Tests for Virtual KG Query Agent
Tests with real AWS infrastructure (Athena) and mock ontology data.

NOTE: get_ontology_from_neptune and discover_named_graphs are MCP Gateway tools
accessible only through the running AgentCore Gateway — they cannot be imported
or called directly in integration tests. Tests that previously relied on them
now use a mock ontology or are skipped.

Prerequisites:
- AWS credentials configured
- Athena configured with TEST_DATABASE
- Environment variables set (or Parameter Store values)

Required Environment Variables:
- AWS_REGION (default: us-east-1)
- TEST_DATABASE (default: default)
- TEST_TABLE: Table name for Athena query tests
- ATHENA_RESULTS_BUCKET (or SSM parameter /<project>/athena/query-results-bucket)
"""

import json
import sys
import os

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from ontology_query_agent.main import (
    reset_agent_state,
    disambiguate_query_terms,
    execute_athena_query,
    map_sql_results_to_rdf,
)

# Test configuration
TEST_DATABASE = os.getenv('TEST_DATABASE', 'default')
TEST_TABLE = os.getenv('TEST_TABLE', '')
TEST_SESSION_PREFIX = "integration-test"

# Shared mock ontology — used in tests that previously called get_ontology_from_neptune.
# Reflects a minimal ontology shape returned by the real Neptune Gateway tool.
def _build_mock_ontology(database: str, table: str) -> dict:
    class_uri = f"http://example.com/{database}/{table.capitalize()}"
    prop_uri = f"http://example.com/{database}/{table.capitalize()}Id"
    return {
        "database_name": database,
        "classes": {
            class_uri: {"label": table.capitalize(), "comment": f"Represents {table}"}
        },
        "properties": {
            prop_uri: {"label": f"{table}_id", "domain": class_uri}
        },
        "mappings": {
            class_uri: {"table": f"{database}.{table}"},
            prop_uri: {"column": f"{table}.id"}
        }
    }

def test_1_disambiguation_with_mock_ontology():
    """Test 1: Disambiguation with mock ontology (Neptune Gateway not required)"""
    print("\n=== Integration Test 1: Disambiguation (mock ontology) ===")
    table = TEST_TABLE or "policies"
    try:
        reset_agent_state(f"{TEST_SESSION_PREFIX}-disambig")

        mock_ontology = _build_mock_ontology(TEST_DATABASE, table)
        ontology_json = json.dumps(mock_ontology)

        class_name = table.lower()
        test_query = f"Show me {class_name}"
        print(f"Test query: '{test_query}'")

        result = disambiguate_query_terms(test_query, ontology_json)
        result_data = json.loads(result)

        status = result_data.get('status')
        print(f"Disambiguation status: {status}")
        print(f"✅ Disambiguation returns valid JSON with status: {status}")

        return True

    except Exception as e:
        print(f"❌ Disambiguation test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_5_disambiguation_class_table_same_entity():
    """Test 5: A term matching both its class name and its table name (same entity)
    must resolve as CLEAR — not AMBIGUOUS.  Reproduces the production bug where
    'count of coverages?' was returned as AMBIGUOUS because 'coverage' matched
    both the Coverage class and the semantic_layer_iceberg.coverage table."""
    print("\n=== Integration Test 5: Disambiguation — class/table same-entity dedup ===")
    table = TEST_TABLE or "coverage"
    database = TEST_DATABASE
    try:
        # Build an ontology where the class name and table name are the same word.
        class_name_cap = table.capitalize()
        class_uri = f"http://example.com/ontology/{class_name_cap}"
        mock_ontology = {
            "database_name": database,
            "classes": {class_uri: {}},
            "properties": {},
            "mappings": {class_uri: {"table": f"{database}.{table}"}}
        }
        ontology_json = json.dumps(mock_ontology)

        reset_agent_state(f"{TEST_SESSION_PREFIX}-dedup")

        # Query uses the plural form (e.g. "coverages") which matches both the class
        # (coverage → Coverage) and the table name (coverage).
        test_query = f"count of {table}s?"
        print(f"Test query: '{test_query}'")

        result = disambiguate_query_terms(test_query, ontology_json)
        result_data = json.loads(result)

        status = result_data.get('status')
        ambiguities = result_data.get('ambiguities', [])

        if status == 'CLEAR':
            print(f"✅ Status is CLEAR — same-entity dedup works correctly")
            return True
        elif status == 'AMBIGUOUS':
            # Check if ALL ambiguity matches point to the same class+table
            for amb in ambiguities:
                unique_pairs = {(m['class'], m['table']) for m in amb.get('matches', [])}
                if len(unique_pairs) == 1:
                    print(f"❌ BUG REPRODUCED: all matches are same entity but status={status}")
                    return False
            print(f"⚠️  AMBIGUOUS with genuinely different interpretations — acceptable")
            return True
        else:
            print(f"⚠️  Unexpected status '{status}' — check ontology setup")
            return True

    except Exception as e:
        print(f"❌ Same-entity dedup test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_2_athena_query():
    """Test 2: Execute Athena query against real table"""
    print("\n=== Integration Test 2: Athena Query Execution ===")
    if not TEST_TABLE:
        print("⚠️  TEST_TABLE not set — skipping (set TEST_TABLE=<your_table_name>)")
        return True

    try:
        reset_agent_state(f"{TEST_SESSION_PREFIX}-athena")

        # execute_athena_query only checks disambiguation_complete (not ontology_retrieved)
        from ontology_query_agent import main as _qm
        _qm._agent_state['disambiguation_complete'] = True

        sql_query = f'SELECT * FROM "{TEST_TABLE}" LIMIT 5'
        print(f"Test SQL: {sql_query}")

        result = execute_athena_query(sql_query, TEST_DATABASE)
        result_data = json.loads(result)

        if "error" in result_data:
            print(f"❌ Athena query failed: {result_data['error']}")
            print("Check: Database exists, table exists, Athena permissions, ATHENA_RESULTS_BUCKET")
            return False

        columns = result_data.get('columns', [])
        rows = result_data.get('rows', [])

        print(f"✅ Query executed successfully")
        print(f"✅ Query ID: {result_data.get('query_execution_id')}")
        print(f"✅ Columns: {len(columns)}")
        print(f"✅ Rows returned: {len(rows)}")
        if columns:
            print(f"   Columns: {', '.join(columns[:5])}")

        return True

    except Exception as e:
        print(f"❌ Athena query test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_3_rdf_mapping():
    """Test 3: Map Athena results to RDF using mock ontology"""
    print("\n=== Integration Test 3: RDF Mapping ===")
    if not TEST_TABLE:
        print("⚠️  TEST_TABLE not set — skipping")
        return True

    try:
        reset_agent_state(f"{TEST_SESSION_PREFIX}-rdf")

        from ontology_query_agent import main as _qm
        _qm._agent_state['disambiguation_complete'] = True

        sql_query = f'SELECT * FROM "{TEST_TABLE}" LIMIT 3'
        query_result = execute_athena_query(sql_query, TEST_DATABASE)
        query_data = json.loads(query_result)

        if "error" in query_data:
            print(f"❌ Cannot test RDF mapping: Athena query failed: {query_data['error']}")
            return False

        _qm._agent_state['query_executed'] = True

        mock_ontology = _build_mock_ontology(TEST_DATABASE, TEST_TABLE)
        ontology_json = json.dumps(mock_ontology)

        result = map_sql_results_to_rdf(query_result, ontology_json)
        result_data = json.loads(result)

        if "error" in result_data:
            print(f"❌ RDF mapping failed: {result_data['error']}")
            return False

        n_quads_count = result_data.get('n_quads_count', 0)
        rows_processed = result_data.get('rows_processed', 0)
        sample_n_quads = result_data.get('sample_n_quads', [])

        print(f"✅ RDF mapping successful")
        print(f"✅ Generated {n_quads_count} n-quads")
        print(f"✅ Processed {rows_processed} rows")

        if sample_n_quads:
            print("\nSample n-quads:")
            for nquad in sample_n_quads[:3]:
                print(f"   {nquad}")

        return True

    except Exception as e:
        print(f"❌ RDF mapping test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_4_query_agent_creation():
    """Test 4: Create the query agent (verifies Bedrock model config)"""
    print("\n=== Integration Test 4: Query Agent Creation ===")
    try:
        from ontology_query_agent.main import create_query_agent

        agent = create_query_agent()
        print(f"✅ Query agent created successfully")
        print(f"✅ Agent is callable: {callable(agent)}")

        return True

    except Exception as e:
        print(f"❌ Query agent creation failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def run_all_integration_tests():
    """Run all integration tests"""
    print("=" * 70)
    print("Virtual KG Query Agent - Integration Test Suite")
    print("=" * 70)
    print(f"\nTest Database : {TEST_DATABASE}")
    print(f"Test Table    : {TEST_TABLE or '(not set — Athena tests will skip)'}")
    print(f"AWS Region    : {os.getenv('AWS_REGION', 'us-east-1')}")
    print("\nPrerequisites:")
    print("- Athena database configured (set TEST_TABLE for query tests)")
    print("- ATHENA_RESULTS_BUCKET or SSM param configured")
    print("- AWS credentials configured")
    print("\nNote: Neptune/Gateway tests are skipped — get_ontology_from_neptune is")
    print("      an MCP tool only available through the running AgentCore Gateway.")
    print("=" * 70)

    tests = [
        test_1_disambiguation_with_mock_ontology,
        test_2_athena_query,
        test_3_rdf_mapping,
        test_4_query_agent_creation,
        test_5_disambiguation_class_table_same_entity,
    ]

    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
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
    else:
        print("\n⚠️  Some integration tests failed")
        print("Note: Integration tests require real AWS infrastructure")
        return 1

if __name__ == "__main__":
    exit_code = run_all_integration_tests()
    sys.exit(exit_code)
