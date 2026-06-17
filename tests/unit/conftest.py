"""
Stub out heavyweight runtime dependencies that are not installed locally.
This conftest is loaded by pytest before any test module is imported.
"""
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock

# Default AWS region for tests that instantiate boto3 clients without an explicit region.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")


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

# bedrock_agentcore.evaluation.runner.dataset_types — the multi-turn eval
# harness (agents/shared/eval_multiturn.build_scenarios) imports
# PredefinedScenario / SimulatedScenario / Turn / ActorProfile at call time.
# Provide real-shaped dataclass stubs (the bare ``bedrock_agentcore`` stub above
# is not a package, so the genuine submodules can't resolve). Field names mirror
# the SDK so build_scenarios constructs them with the same kwargs as production.
from dataclasses import dataclass as _dataclass

_stub("bedrock_agentcore.evaluation")
_stub("bedrock_agentcore.evaluation.runner")
_bac_dataset_types = _stub("bedrock_agentcore.evaluation.runner.dataset_types")


@_dataclass
class Turn:
    input: str
    expected_response: "str | None" = None


@_dataclass
class ActorProfile:
    traits: dict
    context: str
    goal: str


class PredefinedScenario:
    def __init__(self, *, scenario_id, turns, assertions, expected_trajectory,
                 metadata):
        self.scenario_id = scenario_id
        self.turns = turns
        self.assertions = assertions
        self.expected_trajectory = expected_trajectory
        self.metadata = metadata


class SimulatedScenario:
    def __init__(self, *, scenario_id, actor_profile, input, max_turns,
                 assertions, metadata):
        self.scenario_id = scenario_id
        self.actor_profile = actor_profile
        self.input = input
        self.max_turns = max_turns
        self.assertions = assertions
        self.metadata = metadata


_bac_dataset_types.Turn = Turn
_bac_dataset_types.ActorProfile = ActorProfile
_bac_dataset_types.PredefinedScenario = PredefinedScenario
_bac_dataset_types.SimulatedScenario = SimulatedScenario

# bedrock_agentcore.memory.constants — the lessons-memory writer
# (agents/shared/memory_hooks.py) imports ConversationalMessage + MessageRole at
# call time. Provide real-shaped stubs (a dataclass + an enum exposing .value) so
# the production import path works under the stubbed SDK; the session manager is
# injected via the writer's ``manager_factory`` seam, so it needs no stub.
import enum as _enum

_stub("bedrock_agentcore.memory")
_bac_constants = _stub("bedrock_agentcore.memory.constants")


class MessageRole(_enum.Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"


@_dataclass
class ConversationalMessage:
    text: str
    role: MessageRole


_bac_constants.MessageRole = MessageRole
_bac_constants.ConversationalMessage = ConversationalMessage

# strands — the Tier 2 graph workflow uses the REAL multi-agent Graph engine
# (GraphBuilder / MultiAgentBase / MultiAgentResult / Status). We must import
# those genuine submodules from the installed package BEFORE replacing the
# top-level ``strands`` module with a stub (otherwise ``strands.multiagent``
# can't resolve against a non-package stub). After capturing them, we install
# the stub for everything else (Agent / tool / models) and re-attach the real
# multiagent submodules into sys.modules.
_real_mab_base = None
_real_mab_graph = None
try:
    import importlib

    _real_mab_base = importlib.import_module("strands.multiagent.base")
    _real_mab_graph = importlib.import_module("strands.multiagent.graph")
except Exception:  # noqa: BLE001 — package not installed (minimal CI image)
    _real_mab_base = None
    _real_mab_graph = None

_strands = _stub("strands")
_strands.tool = lambda f: f          # @tool becomes a no-op decorator
_strands.Agent = MagicMock()

if _real_mab_base is not None and _real_mab_graph is not None:
    _mab = _stub("strands.multiagent")
    sys.modules["strands.multiagent.base"] = _real_mab_base
    sys.modules["strands.multiagent.graph"] = _real_mab_graph
    _mab.base = _real_mab_base
    _mab.graph = _real_mab_graph
    _mab.GraphBuilder = _real_mab_graph.GraphBuilder
    _mab.MultiAgentBase = _real_mab_base.MultiAgentBase
    _mab.MultiAgentResult = _real_mab_base.MultiAgentResult
    _mab.Status = _real_mab_base.Status
else:
    _mab = _stub("strands.multiagent")
    _mab_base = _stub("strands.multiagent.base")
    _mab_graph = _stub("strands.multiagent.graph")
    _mab_base.MultiAgentBase = object
    _mab_base.MultiAgentResult = MagicMock()
    _mab_base.Status = MagicMock()
    _mab_graph.GraphBuilder = MagicMock()

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
