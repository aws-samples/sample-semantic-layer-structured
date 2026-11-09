"""
Stub out heavyweight runtime dependencies that are not installed locally.
This conftest is loaded by pytest before any test module is imported.
"""
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# Default AWS region for tests that instantiate boto3 clients without an explicit region.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Bind ``agents.shared`` to the REAL source tree before any test module can bind
# it to the synth-time mirror.
#
# The lambda-rest-api stack copies agents/shared/ to lambda/rest-api/agents/shared/
# at synth time (a gitignored build artifact), and 20+ REST API test modules put
# lambda/rest-api/ on sys.path. Both directories contain an ``agents/shared/``
# with an __init__.py, so ``agents.shared`` binds to whichever appears first on
# sys.path at the moment of FIRST import — and once bound, later sys.path edits
# cannot move it. When a REST API test wins that race, every submodule added to
# agents/shared/ since the last synth is invisible, which aborts collection of
# the whole suite with a bare ModuleNotFoundError. Importing it here — conftest
# runs before any test module — makes the binding deterministic and independent
# of whether anyone has synthed recently.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
else:
    sys.path.insert(0, sys.path.pop(sys.path.index(_REPO_ROOT)))
import agents.shared  # noqa: E402,F401  (imported for its side effect: the binding)

assert os.path.dirname(agents.shared.__file__).startswith(_REPO_ROOT + os.sep), (
    f"agents.shared resolved to {agents.shared.__file__}, not the repo tree — "
    "a stale synth mirror is shadowing it"
)


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

# opentelemetry (pulled in by strands internals).
# Only stub when the real package is absent. Since the telemetry-support commit
# the query agents import a tracing bootstrap that calls
# StrandsTelemetry().setup_otlp_exporter(), which needs the real
# opentelemetry.exporter/sdk submodules — a bare stub shadows them and breaks
# collection. agents/requirements.txt (installed locally and in CI) provides
# the real package, so defer to it when importable.
import importlib.util as _importlib_util

if _importlib_util.find_spec("opentelemetry") is None:
    for _otel_mod in (
        "opentelemetry",
        "opentelemetry.instrumentation",
        "opentelemetry.instrumentation.instrumentor",
    ):
        _stub(_otel_mod)


@pytest.fixture(autouse=True)
def _reset_shared_boto_session():
    """Reset the process-wide boto3 session between tests.

    The agents share a single injectable session (agents.shared.boto_session).
    Tests that call ``set_boto_session`` without a monkeypatch restore (e.g. the
    ontology revision-mode invoke test) would otherwise leak a mock session into
    later tests. Clearing the singleton before and after each test keeps them
    isolated.
    """
    try:
        import agents.shared.boto_session as _bs
    except ImportError:  # agents/ on sys.path but not as a package
        import shared.boto_session as _bs  # type: ignore
    _bs._boto_session = None
    yield
    _bs._boto_session = None
