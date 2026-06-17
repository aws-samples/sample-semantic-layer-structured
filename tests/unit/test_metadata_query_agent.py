"""
Unit Tests for Metadata Query Agent (SemanticRAG)

The deployed agent resolves a question with a deterministic Tier 2 Strands graph
(agents/metadata_query_agent/tier2/workflow.py), NOT a free-form ReAct tool loop.
The legacy single-shot ReAct agent (create_metadata_query_agent) and its bespoke
retrieve_kb_context / disambiguate_query_terms @tools have been removed. These
tests therefore cover the surviving graph-only surface:
  * execute_sql_query  — the single Phase-5 model tool
  * the request/response entrypoint `invoke`
  * the editable graph-phase prompts (EXECUTION_PROMPT, JUDGE_PROMPT)
The graph phases themselves have dedicated tests under tests/unit/test_tier2_*.py
and tests/unit/test_rag_*.py.

Run locally:
    python tests/unit/test_metadata_query_agent.py
    # or via pytest:
    pytest tests/unit/test_metadata_query_agent.py -v
"""

import inspect
import os
import sys

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))


def test_1_imports():
    """Test 1: Verify the graph-only public surface imports."""
    print("\n=== Test 1: Imports ===")
    try:
        from metadata_query_agent.main import (
            execute_sql_query,
            reset_agent_state,
            tier2_resolve,
            invoke,
        )
        print("✅ Graph-surface functions imported successfully")

        from metadata_query_agent.token_manager import count_tokens, get_token_status
        token_count = count_tokens("Hello world")
        print(f"✅ token_manager works: {token_count} tokens")

        return True
    except Exception as e:
        print(f"❌ Import failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_2_legacy_react_surface_removed():
    """Test 2: The legacy ReAct agent + its bespoke @tools are gone."""
    print("\n=== Test 2: Legacy ReAct Surface Removed ===")
    try:
        from metadata_query_agent import main as m

        for removed in (
            'create_metadata_query_agent',
            'create_query_agent',
            'retrieve_kb_context',        # was a bespoke @tool; graph uses retrieve_kb_context_structured
            'disambiguate_query_terms',   # graph uses tier2/disambiguation.py
        ):
            assert not hasattr(m, removed), f"{removed} should have been removed"
            print(f"✅ {removed} removed")

        from metadata_query_agent import query_prompts as qp
        assert not hasattr(qp, 'SYSTEM_PROMPT'), "SYSTEM_PROMPT should have been removed"
        print("✅ SYSTEM_PROMPT removed from query_prompts")

        return True
    except Exception as e:
        print(f"❌ Legacy-surface check failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_3_graph_phase_prompts():
    """Test 3: The editable graph-phase prompts are present and non-empty."""
    print("\n=== Test 3: Graph-Phase Prompts ===")
    try:
        from metadata_query_agent.query_prompts import (
            EXECUTION_PROMPT,
            JUDGE_PROMPT,
            QUERY_MODEL_ID,
        )

        for name, val in (
            ('EXECUTION_PROMPT', EXECUTION_PROMPT),
            ('JUDGE_PROMPT', JUDGE_PROMPT),
            ('QUERY_MODEL_ID', QUERY_MODEL_ID),
        ):
            assert isinstance(val, str) and val.strip(), f"{name} must be a non-empty string"
            print(f"✅ {name} set ({len(val)} chars)")

        # Phase 5 prompt governs execute_sql_query; Phase 3 judge emits SliceSufficiency.
        assert 'execute_sql_query' in EXECUTION_PROMPT, \
            "EXECUTION_PROMPT must reference execute_sql_query"
        assert 'SliceSufficiency' in JUDGE_PROMPT, \
            "JUDGE_PROMPT must reference its SliceSufficiency output"
        print("✅ Prompts reference their graph-phase contracts")

        return True
    except Exception as e:
        print(f"❌ Graph-phase prompt test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_4_state_management():
    """Test 4: reset_agent_state seeds a per-session state dict."""
    print("\n=== Test 4: State Management ===")
    try:
        from metadata_query_agent.main import reset_agent_state, _get_state

        reset_agent_state("test-session-mqa-001")
        state = _get_state()
        assert state['current_session'] == "test-session-mqa-001", \
            "current_session not set correctly"
        assert state['query_executed'] is False
        assert 'cached_results' in state
        print("✅ State seeded with expected keys for the session")

        reset_agent_state("test-session-mqa-002")
        state2 = _get_state()
        assert state2['current_session'] == "test-session-mqa-002"
        assert state2['query_executed'] is False, "Flags must reset on a new session"
        print("✅ State resets correctly on new session")

        return True
    except Exception as e:
        print(f"❌ State management test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_5_execute_sql_query_tool():
    """Test 5: execute_sql_query (the sole Phase-5 model tool) is defined with the right signature."""
    print("\n=== Test 5: execute_sql_query Tool ===")
    try:
        from metadata_query_agent.main import execute_sql_query

        assert callable(execute_sql_query), "execute_sql_query must be callable"
        # Strands @tool wraps the function; the original signature is preserved.
        sig = inspect.signature(execute_sql_query)
        params = list(sig.parameters.keys())
        for expected in ('sql_query', 'database_name', 'catalog_id'):
            assert expected in params, f"execute_sql_query must accept {expected}"
        print(f"✅ execute_sql_query signature: {params}")

        return True
    except Exception as e:
        print(f"❌ execute_sql_query tool test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_6_invoke_signature():
    """Test 6: invoke entrypoint has the AgentCore (payload, context) signature."""
    print("\n=== Test 6: invoke Signature ===")
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


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("Metadata Query Agent - Test Suite")
    print("=" * 60)

    tests = [
        test_1_imports,
        test_2_legacy_react_surface_removed,
        test_3_graph_phase_prompts,
        test_4_state_management,
        test_5_execute_sql_query_tool,
        test_6_invoke_signature,
    ]

    results = []
    for test in tests:
        try:
            results.append(test())
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
    print("\n❌ Some tests failed")
    return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
