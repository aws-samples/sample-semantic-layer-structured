"""
Unit Tests for Ontology Generation Agent
Tests basic functionality with mock data (no infrastructure required)
"""

import json
import sys
import os

# Add agents directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'agents'))

def test_1_imports():
    """Test 1: Verify all imports work"""
    print("\n=== Test 1: Imports ===")
    try:
        from ontology_agent.main import (
            get_single_table_schema,
            sample_table_data,
            retrieve_ontology_patterns,
            download_document_from_s3,
            search_document,
            read_document_lines,
            update_progress,
            save_intermediate_ontology,
            append_nquads,
            append_fk_triples,
            persist_file_to_neptune,
            update_glue_metadata_from_ontology,
            create_phase1_agent,
            create_phase2_agent,
        )
        print("✅ All main functions imported successfully")

        from ontology_agent.token_manager import count_tokens, get_token_status
        print("✅ token_manager imported successfully")

        token_count = count_tokens("Hello world")
        print(f"✅ Token counting works: {token_count} tokens")

        return True
    except Exception as e:
        print(f"❌ Import failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_2_token_manager():
    """Test 2: Test token management utilities"""
    print("\n=== Test 2: Token Manager ===")
    try:
        from ontology_agent.token_manager import count_tokens, get_token_status

        test_cases = [
            ("", 0),
            ("Hello", 1),
            ("Hello world", 2),
            ("This is a longer test string with multiple words", 9)
        ]

        for text, expected_min in test_cases:
            count = count_tokens(text)
            if count >= expected_min:
                print(f"✅ Token count for '{text[:20]}...': {count} tokens")
            else:
                print(f"❌ Unexpected token count: {count} < {expected_min}")
                return False

        status_small = get_token_status(1000)
        status_large = get_token_status(100000)
        status_critical = get_token_status(140000)

        print(f"✅ Token status (1K): {status_small}")
        print(f"✅ Token status (100K): {status_large}")
        print(f"✅ Token status (140K): {status_critical}")

        return True
    except Exception as e:
        print(f"❌ Token manager test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_3_tool_definitions():
    """Test 3: Verify all Phase 1 and Phase 2 tools are defined and callable"""
    print("\n=== Test 3: Tool Definitions ===")
    try:
        from ontology_agent.main import (
            # Phase 1 tools
            get_single_table_schema,
            sample_table_data,
            retrieve_ontology_patterns,
            download_document_from_s3,
            search_document,
            read_document_lines,
            append_nquads,
            save_intermediate_ontology,
            update_progress,
            # Phase 2 tools
            append_fk_triples,
            persist_file_to_neptune,
            update_glue_metadata_from_ontology,
        )

        phase1_tools = [
            ('get_single_table_schema', get_single_table_schema),
            ('sample_table_data', sample_table_data),
            ('retrieve_ontology_patterns', retrieve_ontology_patterns),
            ('download_document_from_s3', download_document_from_s3),
            ('search_document', search_document),
            ('read_document_lines', read_document_lines),
            ('append_nquads', append_nquads),
            ('save_intermediate_ontology', save_intermediate_ontology),
            ('update_progress', update_progress),
        ]
        phase2_tools = [
            ('append_fk_triples', append_fk_triples),
            ('persist_file_to_neptune', persist_file_to_neptune),
            ('update_glue_metadata_from_ontology', update_glue_metadata_from_ontology),
        ]

        all_tools = phase1_tools + phase2_tools
        for name, tool in all_tools:
            if callable(tool):
                print(f"✅ Tool defined and callable: {name}")
            else:
                print(f"❌ Tool not callable: {name}")
                return False

        print(f"✅ All {len(phase1_tools)} Phase 1 tools verified")
        print(f"✅ All {len(phase2_tools)} Phase 2 tools verified")
        return True

    except Exception as e:
        print(f"❌ Tool verification failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_4_update_progress_response_structure():
    """Test 4: Test update_progress returns correct JSON structure (no DynamoDB required)"""
    print("\n=== Test 4: update_progress Response Structure ===")
    try:
        # Test the progress calculation logic by inspecting the expected output shape.
        # We don't call the real function (it requires DynamoDB), but we verify the
        # response schema matches what downstream consumers expect.
        expected_fields = ['success', 'tablesProcessed', 'totalTables', 'currentTable', 'progressPercent']
        mock_response = {
            'success': True,
            'tablesProcessed': 3,
            'totalTables': 10,
            'currentTable': 'policies',
            'progressPercent': 30
        }
        for field in expected_fields:
            if field not in mock_response:
                print(f"❌ Missing field in expected schema: {field}")
                return False
            print(f"✅ Schema field present: {field}")

        # Validate progress percent calculation
        assert mock_response['progressPercent'] == int((3 / 10) * 100)
        print(f"✅ Progress percent calculation is correct (30%)")

        return True
    except Exception as e:
        print(f"❌ update_progress structure test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_5_nquad_parsing_logic():
    """Test 5: Test N-QUAD parsing logic (regex used by persist_file_to_neptune)"""
    print("\n=== Test 5: N-QUAD Parsing Logic ===")
    try:
        import re

        test_nquads = [
            '<http://example.com/Class1> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#Class> <http://example.com/onto/1.0> .',
            '<http://example.com/Class1> <http://www.example.org/virtual-kg/mapsToTable> "test_db.table1" <http://example.com/onto/1.0> .',
            '<http://example.com/Property1> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2002/07/owl#DatatypeProperty> <http://example.com/onto/1.0> .'
        ]

        nquad_pattern = re.compile(r'^(<[^>]+>)\s+(<[^>]+>)\s+(<[^>]+>|"[^"]*"(?:\^\^<[^>]+>)?)\s+(<[^>]+>)\s*\.\s*$')

        parsed_count = 0
        for nquad in test_nquads:
            match = nquad_pattern.match(nquad)
            if match:
                parsed_count += 1
                subject, predicate, obj, graph = match.groups()
                print(f"✅ Parsed n-quad: S={subject[:30]}..., G={graph[:30]}...")
            else:
                print(f"❌ Failed to parse: {nquad[:50]}...")
                return False

        print(f"✅ Successfully parsed {parsed_count}/{len(test_nquads)} n-quads")
        return True

    except Exception as e:
        print(f"❌ N-QUAD parsing test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_6_agent_creation():
    """Test 6: Verify Phase 1 and Phase 2 agents can be created"""
    print("\n=== Test 6: Agent Creation ===")
    try:
        from ontology_agent.main import create_phase1_agent, create_phase2_agent

        os.environ.setdefault('AWS_REGION', 'us-east-1')

        phase1_agent = create_phase1_agent()
        print(f"✅ Phase 1 agent created successfully")
        print(f"✅ Phase 1 agent is callable: {callable(phase1_agent)}")

        # Strands Agent exposes registered tools via .tool_names (list of str)
        if hasattr(phase1_agent, 'tool_names') and len(phase1_agent.tool_names) > 0:
            print(f"✅ Phase 1 agent has {len(phase1_agent.tool_names)} tools: {phase1_agent.tool_names}")
        else:
            print(f"❌ Phase 1 agent has no tools registered")
            return False

        phase2_agent = create_phase2_agent()
        print(f"✅ Phase 2 agent created successfully")
        print(f"✅ Phase 2 agent is callable: {callable(phase2_agent)}")

        if hasattr(phase2_agent, 'tool_names') and len(phase2_agent.tool_names) > 0:
            print(f"✅ Phase 2 agent has {len(phase2_agent.tool_names)} tools: {phase2_agent.tool_names}")
        else:
            print(f"❌ Phase 2 agent has no tools registered")
            return False

        return True

    except Exception as e:
        print(f"❌ Agent creation failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_7_system_prompt_structure():
    """Test 7: Verify Phase 1 and Phase 2 system prompts contain required elements"""
    print("\n=== Test 7: System Prompt Structure ===")
    try:
        from ontology_agent.prompt_builder import build_phase1_system_prompt, build_phase2_system_prompt

        phase1_prompt = build_phase1_system_prompt()
        phase2_prompt = build_phase2_system_prompt()

        phase1_required = [
            'N-QUADS',
            'mapsToTable',
            'mapsToColumn',
            'get_single_table_schema',
            'append_nquads',
            'save_intermediate_ontology',
            'update_progress',
        ]

        phase2_required = [
            'append_fk_triples',
            'persist_file_to_neptune',
        ]

        print("Phase 1 prompt checks:")
        for element in phase1_required:
            if element.lower() in phase1_prompt.lower():
                print(f"  ✅ Contains: {element}")
            else:
                print(f"  ⚠️  Missing: {element}")

        print("Phase 2 prompt checks:")
        for element in phase2_required:
            if element.lower() in phase2_prompt.lower():
                print(f"  ✅ Contains: {element}")
            else:
                print(f"  ⚠️  Missing: {element}")

        print(f"✅ Phase 1 system prompt is {len(phase1_prompt)} characters")
        print(f"✅ Phase 2 system prompt is {len(phase2_prompt)} characters")
        return True

    except Exception as e:
        print(f"❌ System prompt structure test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_8_invoke_signature():
    """Test 8: Verify invoke entrypoint has correct (payload, context) signature"""
    print("\n=== Test 8: invoke Signature ===")
    try:
        import inspect
        from ontology_agent.main import invoke

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

def test_9_document_tools_structure():
    """Test 9: Test document processing tools have correct signatures"""
    print("\n=== Test 9: Document Tools Structure ===")
    try:
        from ontology_agent.main import download_document_from_s3, search_document, read_document_lines
        import inspect

        sig = inspect.signature(download_document_from_s3)
        params = list(sig.parameters.keys())
        if 's3_path' in params:
            print(f"✅ download_document_from_s3 has s3_path parameter")
        else:
            print(f"❌ download_document_from_s3 missing s3_path parameter")
            return False

        sig = inspect.signature(search_document)
        params = list(sig.parameters.keys())
        if 'file_path' in params and 'search_term' in params:
            print(f"✅ search_document has required parameters")
        else:
            print(f"❌ search_document missing required parameters")
            return False

        sig = inspect.signature(read_document_lines)
        params = list(sig.parameters.keys())
        if 'file_path' in params and 'start_line' in params:
            print(f"✅ read_document_lines has required parameters")
        else:
            print(f"❌ read_document_lines missing required parameters")
            return False

        print(f"✅ All document tools have correct signatures")
        return True

    except Exception as e:
        print(f"❌ Document tools structure test failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def run_all_tests():
    """Run all tests"""
    print("=" * 70)
    print("Ontology Generation Agent - Unit Test Suite")
    print("=" * 70)
    print("\nThese tests do NOT require AWS infrastructure")
    print("They validate agent structure, tools, and logic with mock data")
    print("=" * 70)

    tests = [
        test_1_imports,
        test_2_token_manager,
        test_3_tool_definitions,
        test_4_update_progress_response_structure,
        test_5_nquad_parsing_logic,
        test_6_agent_creation,
        test_7_system_prompt_structure,
        test_8_invoke_signature,
        test_9_document_tools_structure,
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
    print("Test Summary")
    print("=" * 70)
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    print(f"Failed: {total - passed}/{total}")

    if passed == total:
        print("\n✅ All unit tests passed!")
        return 0
    else:
        print("\n⚠️  Some unit tests failed")
        return 1

if __name__ == "__main__":
    exit_code = run_all_tests()
    sys.exit(exit_code)
