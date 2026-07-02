#!/bin/bash
# =============================================================================
# Red Teaming CI Gate — Semantic Layer Platform
# =============================================================================
# Validates that Bedrock Guardrails hold under adversarial pressure.
# Exit code 0 = all guardrails held; Exit code 1 = breaches detected.
#
# Usage:
#   ./scripts/red-team-ci.sh
#
# Prerequisites:
#   - Python 3.10+ with strands-agents-evals installed
#   - AWS credentials configured (Bedrock model access for Claude)
#   - Agent runtimes deployed (or local agent factories available)
# =============================================================================

set -euo pipefail

echo "========================================================"
echo "  🔴 RED TEAM EVALUATION"
echo "  Targets: metadata_query_agent, ontology_query_agent"
echo "  Strategy: CrescendoStrategy (multi-turn escalation)"
echo "  Threshold: 0 breaches allowed (FSI zero-tolerance)"
echo "========================================================"
echo ""

python -m tests.eval.red_team.run_red_team

echo ""
echo "✅ Red team passed — guardrails held under adversarial pressure"
