"""
Unit Tests for Virtual KG Query Agent
Tests basic functionality with mock data (no infrastructure required)
"""

import json
import sys
import os

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

from ontology_query_agent.main import (
    reset_agent_state,
    disambiguate_query_terms,
    execute_sql_query,
    map_sql_results_to_rdf
)

def test_1_imports():
    """Test 1: Verify all imports work"""
    print("\n=== Test 1: Imports ===")
    try:
        from ontology_query_agent.token_manager import count_tokens
        print("✅ token_manager imported successfully")

        token_count = count_tokens("Hello world")
        print(f"✅ Token counting works: {token_count} tokens")

        return True
    except Exception as e:
        print(f"❌ Import failed: {str(e)}")
        return False

def test_2_state_management():
    """Test 2: Verify state management"""
    print("\n=== Test 2: State Management ===")
    try:
        reset_agent_state("test-session-123")
        print("✅ State reset successful")

        from ontology_query_agent.main import _agent_state
        assert _agent_state['current_session'] == "test-session-123"
        assert _agent_state['ontology_retrieved'] == False
        print("✅ State structure correct")

        return True
    except Exception as e:
        print(f"❌ State management failed: {str(e)}")
        return False

def test_3_tool_definitions():
    """Test 3: Verify local tools are defined (get_ontology_from_neptune is an MCP Gateway tool and is not a local import)"""
    print("\n=== Test 3: Tool Definitions ===")
    try:
        tools = [
            disambiguate_query_terms,
            execute_sql_query,
            map_sql_results_to_rdf
        ]

        for tool in tools:
            print(f"✅ Tool defined: {tool.__name__}")

        print("ℹ️  get_ontology_from_neptune is provided via AgentCore Gateway MCP — not a local import")
        return True
    except Exception as e:
        print(f"❌ Tool verification failed: {str(e)}")
        return False

def test_4_disambiguation_logic():
    """Test 4: Test disambiguation logic with mock data"""
    print("\n=== Test 4: Disambiguation Logic ===")
    try:
        # Create mock ontology
        mock_ontology = {
            "database_name": "test_db",
            "classes": {
                "http://example.com/Policy": {},
                "http://example.com/Customer": {},
                "http://example.com/Party": {}
            },
            "properties": {},
            "mappings": {
                "http://example.com/Policy": {"table": "test_db.policies"},
                "http://example.com/Customer": {"table": "test_db.customers"},
                "http://example.com/Party": {"table": "test_db.party"}
            }
        }

        reset_agent_state("test-disambig")
        ontology_json = json.dumps(mock_ontology)

        # Test clear query (single class, no table collision)
        result = disambiguate_query_terms("Show me policies", ontology_json)
        result_data = json.loads(result)
        print(f"Clear query status: {result_data.get('status')}")
        print(f"✅ Disambiguation returns valid JSON")

        # Test ambiguous query (two distinct classes: Customer vs Party - truly different)
        reset_agent_state("test-disambig-2")
        result2 = disambiguate_query_terms("Show me customers", ontology_json)
        result2_data = json.loads(result2)
        print(f"Single-match query status: {result2_data.get('status')}")
        print(f"✅ Handles single-match queries")

        return True
    except Exception as e:
        print(f"❌ Disambiguation test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_7_disambiguation_false_positive_fix():
    """Test 7: Verify that a term matching BOTH its class name AND its table name
    is resolved as CLEAR (not AMBIGUOUS).  This was the bug observed in production
    where 'coverages' matched class Coverage and table coverage — same entity."""
    print("\n=== Test 7: Disambiguation False-Positive Fix ===")
    try:
        # Ontology where 'coverage' class maps to 'semantic_layer_iceberg.coverage' table.
        # Querying for "coverages" would previously match BOTH the class name
        # (coverage) AND the table name (coverage), returning AMBIGUOUS even though
        # both interpretations resolve to the same (class, table) pair.
        mock_ontology = {
            "database_name": "semantic_layer_iceberg",
            "classes": {
                "http://example.com/ontology/Coverage": {}
            },
            "properties": {},
            "mappings": {
                "http://example.com/ontology/Coverage": {"table": "semantic_layer_iceberg.coverage"}
            }
        }

        reset_agent_state("test-false-positive")
        ontology_json = json.dumps(mock_ontology)

        result = disambiguate_query_terms("count of coverages?", ontology_json)
        result_data = json.loads(result)

        status = result_data.get('status')
        ambiguities = result_data.get('ambiguities', [])

        if status == 'CLEAR':
            print(f"✅ Status correctly resolved as CLEAR (not AMBIGUOUS)")
            mappings = result_data.get('mappings', {})
            coverage_mapping = mappings.get('coverages') or mappings.get('coverage') or {}
            print(f"✅ Mapping: {coverage_mapping}")
            return True
        else:
            print(f"❌ Unexpected status '{status}' — expected CLEAR")
            print(f"   Ambiguities: {ambiguities}")
            return False

    except Exception as e:
        print(f"❌ False-positive fix test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_5_invoke_signature():
    """Test 5: Verify invoke entrypoint has correct (payload, context) signature"""
    print("\n=== Test 5: invoke Signature ===")
    try:
        import inspect
        from ontology_query_agent.main import invoke

        sig = inspect.signature(invoke)
        params = list(sig.parameters.keys())

        if 'payload' in params and 'context' in params:
            print(f"✅ invoke has correct signature: {params}")
        else:
            print(f"❌ invoke signature mismatch — expected (payload, context), got {params}")
            return False

        return True
    except Exception as e:
        print(f"❌ invoke signature test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_6_agent_creation():
    """Test 6: Verify agent can be created"""
    print("\n=== Test 6: Agent Creation ===")
    try:
        from ontology_query_agent.main import create_query_agent
        agent = create_query_agent()
        print(f"✅ Agent created successfully")
        print(f"✅ Agent is callable: {callable(agent)}")

        return True
    except Exception as e:
        print(f"❌ Agent creation failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_8_query_answer_structured_output():
    """Test 8: Verify QueryAnswer Pydantic model validates structured output correctly."""
    print("\n=== Test 8: QueryAnswer Structured Output Model ===")
    try:
        from pydantic import ValidationError
        from ontology_query_agent.query_prompts import QueryAnswer

        qa = QueryAnswer(answer="There are 42 active policies in New York state.")
        assert qa.answer == "There are 42 active policies in New York state."
        print("✅ Valid plain-English answer accepted")

        qa2 = QueryAnswer(answer="The top customer is Acme Corp. They have 12 active policies.")
        assert isinstance(qa2.answer, str) and len(qa2.answer) > 0
        print("✅ Multi-sentence answer accepted")

        try:
            QueryAnswer()
            print("❌ Should have raised ValidationError for missing 'answer'")
            return False
        except ValidationError:
            print("✅ ValidationError raised when 'answer' field is missing")

        try:
            QueryAnswer(answer=None)
            print("❌ Should have raised ValidationError for None answer")
            return False
        except (ValidationError, TypeError):
            print("✅ ValidationError raised for None answer")

        qa_empty = QueryAnswer(answer="")
        assert qa_empty.answer == ""
        print("✅ Empty string accepted by model (content enforcement is prompt-level)")

        schema = QueryAnswer.model_json_schema()
        assert 'answer' in schema.get('properties', {}), "Expected 'answer' in schema properties"
        assert 'answer' in schema.get('required', []), "Expected 'answer' in schema required list"
        print(f"✅ JSON schema correct: {schema}")

        return True

    except Exception as e:
        print(f"❌ QueryAnswer structured output test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_parse_agent_response_needs_clarification():
    """query_service detects needs_clarification and surfaces options."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'lambda', 'rest-api'))
    from services.query_service import QueryService
    qs = QueryService.__new__(QueryService)
    raw = json.dumps({
        "needs_clarification": True,
        "clarification_question": "Which policy?",
        "options": [
            {"id": "policy_master", "label": "Insurance Policy (policy_master)"},
            {"id": "coverage", "label": "Coverage Agreement (coverage)"},
        ]
    })
    result = qs._parse_agent_response(raw)
    assert result["needs_clarification"] is True
    assert len(result["options"]) == 2
    assert result["answer"] == ""


def test_disambiguate_resolves_via_sparql_context():
    """SPARQL context confirms a class mapping, boosting confidence."""
    from ontology_query_agent.main import disambiguate_query_terms, reset_agent_state
    reset_agent_state("t4")
    ontology = json.dumps({
        "classes": {"http://ex.com/Customer": {}},
        "properties": {},
        "mappings": {"http://ex.com/Customer": {"table": "db.customers"}},
    })
    sparql_ctx = json.dumps({
        "results": [{"class": "http://ex.com/Customer", "table": "db.customers"}]
    })
    result = json.loads(disambiguate_query_terms(
        "list customers", ontology, '{}', sparql_ctx
    ))
    assert result["status"] == "CLEAR"
    m = result["mappings"].get("customer") or result["mappings"].get("customers")
    assert m is not None
    assert m.get("confidence", 0) >= 0.9



def run_all_tests():
    """Run all tests"""
    print("=" * 60)
    print("Virtual KG Query Agent - Test Suite")
    print("=" * 60)

    tests = [
        test_1_imports,
        test_2_state_management,
        test_3_tool_definitions,
        test_4_disambiguation_logic,
        test_5_invoke_signature,
        test_6_agent_creation,
        test_7_disambiguation_false_positive_fix,
        test_8_query_answer_structured_output,
        test_retrieve_kb_context_no_kb_id,
        test_disambiguate_resolves_synonym_via_kb,
        test_disambiguate_resolves_via_sparql_context,
        test_parse_agent_response_needs_clarification,
    ]

    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"❌ Test crashed: {str(e)}")
            results.append(False)

    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    print(f"Failed: {total - passed}/{total}")

    if passed == total:
        print("\n✅ All tests passed!")
        return 0
    else:
        print("\n❌ Some tests failed")
        return 1

if __name__ == "__main__":
    exit_code = run_all_tests()
    sys.exit(exit_code)
