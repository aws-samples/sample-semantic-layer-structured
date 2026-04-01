"""
Stub out heavyweight runtime dependencies that are not installed locally.
This conftest is loaded by pytest before any test module is imported.
"""
import sys
from types import ModuleType
from unittest.mock import MagicMock


def _stub(name: str) -> ModuleType:
    mod = ModuleType(name)
    sys.modules[name] = mod
    return mod


# bedrock_agentcore
_bac = _stub("bedrock_agentcore")


# Mock BedrockAgentCoreApp with an entrypoint decorator that returns the function as-is
class MockBedrockAgentCoreApp:
    def __init__(self, debug=False):
        pass

    def entrypoint(self, func):
        """Decorator that returns the original function (for testing)"""
        return func

    def add_async_task(self, name, data):
        """Mock method that returns a task ID"""
        return MagicMock()

    def complete_async_task(self, task_id):
        """Mock method"""
        pass

    def run(self):
        """Mock method"""
        pass


_bac.BedrockAgentCoreApp = MockBedrockAgentCoreApp
# bedrock_agentcore.runtime also exposes BedrockAgentCoreApp (used by metadata_query_agent)
_bac_runtime = _stub("bedrock_agentcore.runtime")
_bac_runtime.BedrockAgentCoreApp = MockBedrockAgentCoreApp
_stub("bedrock_agentcore.runtime.app")

# strands
_strands = _stub("strands")
_strands.tool = lambda f: f          # @tool becomes a no-op decorator
_strands.Agent = MagicMock()

_strands_agent = _stub("strands.agent")
_strands_conv = _stub("strands.agent.conversation_manager")
_strands_conv.SlidingWindowConversationManager = MagicMock()

_strands_models = _stub("strands.models")
_strands_models.BedrockModel = MagicMock()

_strands_tools = _stub("strands.tools")
_strands_tools_mcp = _stub("strands.tools.mcp")
_strands_tools_mcp.MCPClient = MagicMock()

_strands_types = _stub("strands.types")
_strands_exc = _stub("strands.types.exceptions")
_strands_exc.MaxTokensReachedException = Exception

# mcp_proxy_for_aws
_mcp = _stub("mcp_proxy_for_aws")
_mcp_client = _stub("mcp_proxy_for_aws.client")
_mcp_client.aws_iam_streamablehttp_client = MagicMock()

# opentelemetry (pulled in by strands internals)
for _otel_mod in (
    "opentelemetry",
    "opentelemetry.instrumentation",
    "opentelemetry.instrumentation.instrumentor",
):
    _stub(_otel_mod)
