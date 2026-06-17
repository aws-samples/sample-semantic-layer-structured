"""
Integration Tests for Ontology Agent
Tests with real AWS infrastructure (Athena + Neptune via Gateway + S3)

Prerequisites:
- AWS credentials configured
- Athena table accessible (set TEST_TABLE and TEST_CATALOG_ID env vars)
- Neptune Gateway running (for persist_file_to_neptune)
- Environment variables set (or Parameter Store values)

Required Environment Variables:
- AWS_REGION (default: us-east-1)
- TEST_DATABASE (default: default)
- TEST_TABLE: A table name that exists in TEST_DATABASE
- TEST_CATALOG_ID: Catalog ID for the table (e.g. 'AWSDataCatalog' or 's3tablescatalog/<bucket>')
- NEPTUNE_GATEWAY_URL (for persist_file_to_neptune test)
- ARTIFACTS_BUCKET (for S3 persistence test)
"""

import json
import sys
import os
import tempfile

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from ontology_agent.main import (
    get_single_table_schema,
    download_document_from_s3,
    search_document,
    save_ontology_to_s3,
    create_phase1_agent,
    create_phase2_agent,
)

# Test configuration
TEST_DATABASE = os.getenv('TEST_DATABASE', 'default')
TEST_TABLE = os.getenv('TEST_TABLE', '')
TEST_CATALOG_ID = os.getenv('TEST_CATALOG_ID', 'AWSDataCatalog')
TEST_SESSION_PREFIX = "integration-test-ontology"

def test_1_athena_connectivity():
    """Test 1: Verify Athena connectivity via get_single_table_schema"""
    print("\n=== Integration Test 1: Athena Connectivity ===")
    if not TEST_TABLE:
        print("⚠️  TEST_TABLE not set — skipping (set TEST_TABLE=<your_table_name>)")
        return True

    try:
        print(f"Attempting to describe table: {TEST_DATABASE}.{TEST_TABLE} (catalog: {TEST_CATALOG_ID})")
        result = get_single_table_schema(TEST_DATABASE, TEST_TABLE, TEST_CATALOG_ID)
        result_data = json.loads(result)

        if "error" in result_data:
            print(f"❌ Schema retrieval failed: {result_data['error']}")
            print("Check: Table exists, Athena permissions, catalog ID")
            return False

        columns = result_data.get('columns', [])
        print(f"✅ Connected to Athena successfully")
        print(f"✅ Table: {result_data.get('table_name')} has {len(columns)} columns")

        for col in columns[:3]:
            print(f"   - {col['name']} ({col['type']})")

        return True

    except Exception as e:
        print(f"❌ Athena connectivity test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_2_table_schema_retrieval():
    """Test 2: Retrieve table schema via Athena DESCRIBE TABLE"""
    print("\n=== Integration Test 2: Table Schema Retrieval ===")
    if not TEST_TABLE:
        print("⚠️  TEST_TABLE not set — skipping")
        return True

    try:
        print(f"Retrieving schema for: {TEST_DATABASE}.{TEST_TABLE}")
        schema_result = get_single_table_schema(TEST_DATABASE, TEST_TABLE, TEST_CATALOG_ID)
        schema_data = json.loads(schema_result)

        if "error" in schema_data:
            print(f"❌ Schema retrieval failed: {schema_data['error']}")
            return False

        columns = schema_data.get('columns', [])
        token_estimate = schema_data.get('token_estimate', 0)

        print(f"✅ Retrieved schema for {TEST_TABLE}")
        print(f"✅ Columns: {len(columns)}")
        print(f"✅ Token estimate: {token_estimate}")

        for col in columns[:3]:
            print(f"   - {col['name']} ({col['type']})")

        return True

    except Exception as e:
        print(f"❌ Table schema test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_3_token_counting():
    """Test 3: Token counting functionality"""
    print("\n=== Integration Test 3: Token Counting ===")
    try:
        from ontology_agent.token_manager import count_tokens, get_token_status

        test_text = "This is a test of the token counting functionality. " * 10
        print(f"Test text length: {len(test_text)} characters")

        token_count = count_tokens(test_text)
        status = get_token_status(token_count)

        if token_count > 0:
            print(f"✅ Token counting works")
            print(f"✅ Token count: {token_count}")
            print(f"✅ Status: {status}")
            return True
        else:
            print(f"❌ Token count returned zero")
            return False

    except Exception as e:
        print(f"❌ Token counting test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_4_neptune_persistence_via_file():
    """Test 4: Persist N-Quads to Neptune via persist_file_to_neptune (file-based)"""
    print("\n=== Integration Test 4: Neptune Persistence (file-based) ===")
    neptune_gateway_url = os.getenv('NEPTUNE_GATEWAY_URL', '')
    if not neptune_gateway_url:
        print("⚠️  NEPTUNE_GATEWAY_URL not set — skipping")
        return True

    try:
        from ontology_agent.main import persist_file_to_neptune

        test_nquads = """<http://example.com/test/TestClass> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <http://example.com/test/ontology/1.0.0> .
<http://example.com/test/TestClass> <http://www.example.org/virtual-kg/mapsToTable> "test_db.test_table" <http://example.com/test/ontology/1.0.0> .
<http://example.com/test/TestProperty> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <http://example.com/test/ontology/1.0.0> ."""

        # persist_file_to_neptune reads from /tmp/ontologies/<ontology_id>/<table_name>.md
        test_ontology_id = "integration-test-999"
        test_table_name = "test_table"

        # Use secure temporary directory instead of hardcoded /tmp
        import shutil
        tmpdir = tempfile.mkdtemp()
        try:
            nq_dir = os.path.join(tmpdir, "ontologies", test_ontology_id)
            os.makedirs(nq_dir, exist_ok=True)
            nq_file = os.path.join(nq_dir, f"{test_table_name}.nq")

            with open(nq_file, 'w', encoding='utf-8') as f:
                f.write(test_nquads)

            print(f"Written test N-Quads to: {nq_file}")
            result = persist_file_to_neptune(test_ontology_id, test_table_name)
            result_data = json.loads(result)

            if result_data.get('success'):
                print(f"✅ Neptune persistence successful")
                print(f"✅ Triples persisted: {result_data.get('triples_persisted', '?')}")
                return True
            else:
                print(f"❌ Neptune persistence failed: {result_data.get('error', result)}")
                print("Check: Neptune Gateway URL, IAM permissions, network access")
                return False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    except Exception as e:
        print(f"❌ Neptune persistence test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_5_s3_persistence():
    """Test 5: Save ontology to S3"""
    print("\n=== Integration Test 5: S3 Persistence ===")
    if not os.getenv('ARTIFACTS_BUCKET'):
        print("⚠️  ARTIFACTS_BUCKET not set — skipping (set ARTIFACTS_BUCKET=<your_bucket>)")
        return True

    try:
        test_nquads = """<http://example.com/test/TestClass> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <http://example.com/test/ontology/1.0.0> .
<http://example.com/test/TestProperty> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <http://example.com/test/ontology/1.0.0> ."""

        print("Attempting to save test N-Quads to S3...")

        result = save_ontology_to_s3(
            ontology_content=test_nquads,
            ontology_id="integration-test-s3",
            filename="test_ontology.nq"
        )
        result_data = json.loads(result)

        if result_data.get('success'):
            print(f"✅ S3 persistence successful")
            print(f"✅ Location: {result_data.get('s3_location')}")
            return True
        else:
            print(f"❌ S3 persistence failed: {result_data.get('message')}")
            return False

    except Exception as e:
        print(f"❌ S3 persistence test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_6_phase1_agent_invocation():
    """Test 6: Create Phase 1 agent and verify invocation pattern (mock prompt)"""
    print("\n=== Integration Test 6: Phase 1 Agent Invocation ===")
    if not TEST_TABLE:
        print("⚠️  TEST_TABLE not set — skipping (set TEST_TABLE=<your_table_name>)")
        return True

    try:
        print("Step 1: Creating Phase 1 agent...")
        agent = create_phase1_agent()
        print(f"✅ Phase 1 agent created")
        print(f"✅ Tools: {[t.__name__ if hasattr(t, '__name__') else str(t) for t in agent.tools]}")

        test_ontology_id = "integration-test-phase1-001"
        namespace = f"http://example.com/integration-test/ontology/1.0.0"

        user_prompt = f"""
Generate an OWL ontology with the following configuration:

DATABASE: {TEST_DATABASE}
ONTOLOGY_ID: {test_ontology_id}
ONTOLOGY_NAME: integration_test
NAMESPACE: {namespace}
CATALOG_ID: {TEST_CATALOG_ID}
TOTAL_TABLES: 1
TABLES:
  - {TEST_TABLE}

INSTRUCTIONS:
1. Process ONLY the table listed above
2. Call get_single_table_schema(database_name="{TEST_DATABASE}", table_name="{TEST_TABLE}", catalog_id="{TEST_CATALOG_ID}")
3. Generate N-QUADS format with Virtual KG traceability mappings using namespace {namespace}
4. Call save_intermediate_ontology() to save the generated N-Quads
5. Call update_progress() to mark the table as processed

Generate the ontology now.
"""

        print(f"✅ Prompt built ({len(user_prompt)} characters)")
        print("⏳ Invoking Phase 1 agent (this may take 30-90 seconds)...")

        response = agent(user_prompt)

        print(f"\n✅ Phase 1 agent invocation completed!")
        response_str = str(response.message['content'][0]['text'] if hasattr(response, 'message') else response)
        print(f"✅ Response length: {len(response_str)} characters")

        success_indicators = ['mapsToTable', 'mapsToColumn', TEST_TABLE, 'save_intermediate_ontology']
        found = [ind for ind in success_indicators if ind.lower() in response_str.lower()]
        print(f"✅ Found {len(found)}/{len(success_indicators)} success indicators: {found}")

        print("\n--- Response Preview (first 500 chars) ---")
        print(response_str[:500])
        if len(response_str) > 500:
            print(f"... ({len(response_str) - 500} more characters)")

        if len(found) >= 2:
            print("\n✅ Phase 1 agent invocation test PASSED")
            return True
        else:
            print("\n⚠️  Phase 1 invocation completed but response unclear")
            return False

    except Exception as e:
        print(f"❌ Phase 1 agent invocation test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def run_all_integration_tests():
    """Run all integration tests"""
    print("=" * 70)
    print("Ontology Agent - Integration Test Suite")
    print("=" * 70)
    print(f"\nTest Database : {TEST_DATABASE}")
    print(f"Test Table    : {TEST_TABLE or '(not set — some tests will skip)'}")
    print(f"Test Catalog  : {TEST_CATALOG_ID}")
    print(f"AWS Region    : {os.getenv('AWS_REGION', 'us-east-1')}")
    print("\nPrerequisites:")
    print("- Athena accessible with TEST_TABLE/TEST_CATALOG_ID configured")
    print("- NEPTUNE_GATEWAY_URL set for persistence test")
    print("- ARTIFACTS_BUCKET set for S3 test")
    print("- AWS credentials configured")
    print("=" * 70)

    tests = [
        test_1_athena_connectivity,
        test_2_table_schema_retrieval,
        test_3_token_counting,
        test_4_neptune_persistence_via_file,
        test_5_s3_persistence,
        test_6_phase1_agent_invocation,
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
