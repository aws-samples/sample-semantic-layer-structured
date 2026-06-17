"""
Unit Tests for Virtual KG (Ontology) Query Agent

The deployed VKG agent resolves a question with a deterministic Tier 2 Strands
graph (agents/ontology_query_agent/tier2/workflow.py): it fetches the ontology,
builds an ontology slice, disambiguates, generates SPARQL, then Phase 5
translates that SPARQL to SQL (Ontop) and runs it on Athena DIRECTLY. The legacy
single-shot ReAct agent (create_query_agent) and its bespoke
disambiguate_query_terms / execute_sql_query / map_sql_results_to_rdf @tools (and
the QueryAnswer structured-output model) have been removed. These tests cover the
surviving graph-only surface; the graph phases have dedicated tests under
tests/unit/ (slice builder, query generator, grounding, etc.).
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
        from ontology_query_agent.main import (
            reset_agent_state,
            tier2_resolve,
            _run_athena_sql,
            invoke,
        )
        print("✅ Graph-surface functions imported successfully")

        from ontology_query_agent.token_manager import count_tokens
        token_count = count_tokens("Hello world")
        print(f"✅ token_manager works: {token_count} tokens")

        return True
    except Exception as e:
        print(f"❌ Import failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_2_legacy_react_surface_removed():
    """Test 2: The legacy ReAct agent + its bespoke @tools + QueryAnswer are gone."""
    print("\n=== Test 2: Legacy ReAct Surface Removed ===")
    try:
        from ontology_query_agent import main as m

        for removed in (
            'create_query_agent',
            'disambiguate_query_terms',
            'execute_sql_query',
            'map_sql_results_to_rdf',
        ):
            assert not hasattr(m, removed), f"{removed} should have been removed"
            print(f"✅ {removed} removed")

        from ontology_query_agent import query_prompts as qp
        for removed in ('SYSTEM_PROMPT', 'QueryAnswer', 'EXECUTION_PROMPT',
                        'SPARQL_FALLBACK_SYSTEM_PROMPT'):
            assert not hasattr(qp, removed), f"{removed} should have been removed"
            print(f"✅ {removed} removed from query_prompts")

        return True
    except Exception as e:
        print(f"❌ Legacy-surface check failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_3_model_id_constants():
    """Test 3: The model-id constants the graph consumes are present."""
    print("\n=== Test 3: Model-ID Constants ===")
    try:
        from ontology_query_agent.query_prompts import QUERY_MODEL_ID, JUDGE_MODEL_ID

        for name, val in (('QUERY_MODEL_ID', QUERY_MODEL_ID),
                          ('JUDGE_MODEL_ID', JUDGE_MODEL_ID)):
            assert isinstance(val, str) and val.strip(), f"{name} must be a non-empty string"
            # Must be a full Bedrock model/inference-profile id, not a bare name.
            assert '.' in val, f"{name} must be a full Bedrock identifier, got {val!r}"
            print(f"✅ {name} = {val}")

        return True
    except Exception as e:
        print(f"❌ Model-id constant test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_4_state_management():
    """Test 4: reset_agent_state seeds a per-session marker."""
    print("\n=== Test 4: State Management ===")
    try:
        from ontology_query_agent.main import reset_agent_state

        reset_agent_state("test-session-123")
        from ontology_query_agent.main import _agent_state
        assert _agent_state['current_session'] == "test-session-123"
        assert 'cached_results' in _agent_state
        print("✅ State seeded with session marker + cached_results")

        return True
    except Exception as e:
        print(f"❌ State management failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_5_invoke_signature():
    """Test 5: invoke entrypoint has the (payload, context) signature."""
    print("\n=== Test 5: invoke Signature ===")
    try:
        from ontology_query_agent.main import invoke

        sig = inspect.signature(invoke)
        params = list(sig.parameters.keys())
        assert 'payload' in params and 'context' in params, \
            f"invoke signature mismatch — expected (payload, context), got {params}"
        print(f"✅ invoke has correct signature: {params}")

        return True
    except Exception as e:
        print(f"❌ invoke signature test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_6_run_athena_sql_signature():
    """Test 6: _run_athena_sql (the Phase-5 deterministic Athena core) is keyword-only."""
    print("\n=== Test 6: _run_athena_sql Signature ===")
    try:
        from ontology_query_agent.main import _run_athena_sql

        sig = inspect.signature(_run_athena_sql)
        params = list(sig.parameters.keys())
        for expected in ('sql', 'database_name', 'catalog_id'):
            assert expected in params, f"_run_athena_sql must accept {expected}"
        print(f"✅ _run_athena_sql signature: {params}")

        return True
    except Exception as e:
        print(f"❌ _run_athena_sql signature test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("Virtual KG Query Agent - Test Suite")
    print("=" * 60)

    tests = [
        test_1_imports,
        test_2_legacy_react_surface_removed,
        test_3_model_id_constants,
        test_4_state_management,
        test_5_invoke_signature,
        test_6_run_athena_sql_signature,
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
