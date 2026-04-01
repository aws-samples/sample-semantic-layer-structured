"""
Unit Tests for Metadata Query Agent
Tests basic functionality with mock data (no infrastructure required)

Run locally:
    cd /Users/huthmac/Documents/AWS/00_workspace/semantic-layer
    python tests/unit/test_metadata_query_agent.py
    # or via pytest:
    pytest tests/unit/test_metadata_query_agent.py -v
"""

import inspect
import json
import os
import sys

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))


def test_1_imports():
    """Test 1: Verify all imports work"""
    print("\n=== Test 1: Imports ===")
    try:
        from metadata_query_agent.main import (
            retrieve_kb_context,
            disambiguate_query_terms,
            execute_sql_query,
            reset_agent_state,
            create_metadata_query_agent,
            invoke,
        )
        print("✅ All main functions imported successfully")

        from metadata_query_agent.token_manager import count_tokens, get_token_status
        print("✅ token_manager imported successfully")

        token_count = count_tokens("Hello world")
        print(f"✅ Token counting works: {token_count} tokens")

        return True
    except Exception as e:
        print(f"❌ Import failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_2_query_prompts():
    """Test 2: Verify query prompts are configured"""
    print("\n=== Test 2: Query Prompts ===")
    try:
        from metadata_query_agent.query_prompts import SYSTEM_PROMPT, QUERY_MODEL_ID

        assert isinstance(SYSTEM_PROMPT, str) and len(SYSTEM_PROMPT) > 0, \
            "SYSTEM_PROMPT must be a non-empty string"
        print(f"✅ SYSTEM_PROMPT set ({len(SYSTEM_PROMPT)} chars)")

        assert isinstance(QUERY_MODEL_ID, str) and len(QUERY_MODEL_ID) > 0, \
            "QUERY_MODEL_ID must be a non-empty string"
        print(f"✅ QUERY_MODEL_ID set: {QUERY_MODEL_ID}")

        # Verify the prompt includes the expected tool names
        for tool_name in ['retrieve_kb_context', 'disambiguate_query_terms', 'execute_sql_query']:
            assert tool_name in SYSTEM_PROMPT, f"SYSTEM_PROMPT must reference {tool_name}"
        print("✅ SYSTEM_PROMPT references all 3 tool names")

        return True
    except Exception as e:
        print(f"❌ Query prompts test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_3_state_management():
    """Test 3: Verify agent state management"""
    print("\n=== Test 3: State Management ===")
    try:
        from metadata_query_agent.main import reset_agent_state, _agent_state

        reset_agent_state("test-session-mqa-001")
        print("✅ State reset successful")

        from metadata_query_agent.main import _agent_state
        assert _agent_state['current_session'] == "test-session-mqa-001", \
            "current_session not set correctly"
        assert _agent_state['kb_context_retrieved'] == False
        assert _agent_state['disambiguation_complete'] == False
        assert _agent_state['query_executed'] == False
        assert 'cached_results' in _agent_state
        print("✅ State structure correct — all expected keys present")

        # Reset with different session
        reset_agent_state("test-session-mqa-002")
        from metadata_query_agent.main import _agent_state
        assert _agent_state['current_session'] == "test-session-mqa-002"
        assert _agent_state['kb_context_retrieved'] == False, \
            "State flags must reset to False on new session"
        print("✅ State resets correctly on new session")

        return True
    except Exception as e:
        print(f"❌ State management test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_4_tool_definitions():
    """Test 4: Verify all tools are defined and callable"""
    print("\n=== Test 4: Tool Definitions ===")
    try:
        from metadata_query_agent.main import (
            retrieve_kb_context,
            disambiguate_query_terms,
            execute_sql_query,
        )

        tools = [retrieve_kb_context, disambiguate_query_terms, execute_sql_query]
        for tool in tools:
            assert callable(tool), f"{tool.__name__} must be callable"
            print(f"✅ Tool defined and callable: {tool.__name__}")

        # Verify retrieve_kb_context signature
        sig = inspect.signature(retrieve_kb_context)
        params = list(sig.parameters.keys())
        assert 'user_query' in params, "retrieve_kb_context must accept user_query"
        print(f"✅ retrieve_kb_context signature: {params}")

        # Verify disambiguate_query_terms signature
        sig = inspect.signature(disambiguate_query_terms)
        params = list(sig.parameters.keys())
        assert 'user_query' in params, "disambiguate_query_terms must accept user_query"
        assert 'kb_context' in params, "disambiguate_query_terms must accept kb_context"
        print(f"✅ disambiguate_query_terms signature: {params}")

        # Verify execute_sql_query signature
        sig = inspect.signature(execute_sql_query)
        params = list(sig.parameters.keys())
        assert 'sql_query' in params, "execute_sql_query must accept sql_query"
        assert 'database_name' in params, "execute_sql_query must accept database_name"
        assert 'catalog_id' in params, "execute_sql_query must accept catalog_id"
        print(f"✅ execute_sql_query signature: {params}")

        return True
    except Exception as e:
        print(f"❌ Tool definitions test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_5_invoke_signature():
    """Test 5: Verify invoke entrypoint has correct (payload, context) signature"""
    print("\n=== Test 5: invoke Signature ===")
    try:
        from metadata_query_agent.main import invoke

        sig = inspect.signature(invoke)
        params = list(sig.parameters.keys())

        assert 'payload' in params and 'context' in params, \
            f"invoke signature mismatch — expected (payload, context), got {params}"
        print(f"✅ invoke has correct AgentCore signature: {params}")

        return True
    except Exception as e:
        print(f"❌ invoke signature test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_6_agent_creation():
    """Test 6: Verify metadata query agent can be created"""
    print("\n=== Test 6: Agent Creation ===")
    try:
        from metadata_query_agent.main import create_metadata_query_agent
        agent = create_metadata_query_agent()
        print(f"✅ Metadata query agent created successfully")
        assert callable(agent), "Agent must be callable"
        print(f"✅ Agent is callable")

        return True
    except Exception as e:
        print(f"❌ Agent creation failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_7_retrieve_kb_context_no_kb_id():
    """Test 7: retrieve_kb_context returns error JSON (not exception) when BEDROCK_KB_ID unset"""
    print("\n=== Test 7: KB Context — No KB ID ===")
    try:
        os.environ.pop('BEDROCK_KB_ID', None)
        from metadata_query_agent.main import retrieve_kb_context, reset_agent_state

        reset_agent_state("test-no-kb-id")
        result = json.loads(retrieve_kb_context("show active policies"))
        assert "error" in result, "Must return error JSON when BEDROCK_KB_ID not set"
        print(f"✅ Returns error JSON when BEDROCK_KB_ID unset: {result['error'][:50]}")

        return True
    except Exception as e:
        print(f"❌ KB context no-ID test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_8_disambiguation_with_mock_kb():
    """Test 8: Disambiguation resolves terms from KB context metadata"""
    print("\n=== Test 8: Disambiguation with Mock KB Context ===")
    try:
        from metadata_query_agent.main import disambiguate_query_terms, reset_agent_state

        reset_agent_state("test-disambig-mqa")

        # Build mock KB context with table metadata
        mock_kb_context = json.dumps({
            "query": "show policies",
            "kb_id": "test-kb",
            "documents_retrieved": 1,
            "context": [
                {
                    "content": "policy_master contains active and expired insurance policies",
                    "metadata": {
                        "table_name": "policy_master",
                        "database_name": "insurance_db",
                        "catalog_id": "AWSDataCatalog"
                    },
                    "score": 0.9
                }
            ]
        })

        result = disambiguate_query_terms("show policies", mock_kb_context)
        result_data = json.loads(result)
        assert "status" in result_data, "Result must contain 'status' key"
        print(f"✅ Disambiguation returns valid JSON with status: {result_data.get('status')}")

        return True
    except Exception as e:
        print(f"❌ Disambiguation test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def run_all_tests():
    """Run all tests"""
    print("=" * 60)
    print("Metadata Query Agent - Test Suite")
    print("=" * 60)

    tests = [
        test_1_imports,
        test_2_query_prompts,
        test_3_state_management,
        test_4_tool_definitions,
        test_5_invoke_signature,
        test_6_agent_creation,
        test_7_retrieve_kb_context_no_kb_id,
        test_8_disambiguation_with_mock_kb,
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
    passed = sum(1 for r in results if r)
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
