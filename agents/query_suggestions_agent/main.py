"""
Query Suggestions Agent
Retrieves KB schema context and generates relevant suggested questions.
Returns synchronously — no Athena execution, no polling needed.
"""
import os
import json
import logging
import boto3
import uuid
from bedrock_agentcore import BedrockAgentCoreApp
try:
    from opentelemetry import baggage as _otel_baggage
    from opentelemetry import context as _otel_context
except ImportError:
    _otel_baggage = None  # type: ignore
    _otel_context = None  # type: ignore
from strands import Agent, tool
from strands.models import BedrockModel
from boto3.dynamodb.conditions import Key
from typing import Dict, Any, Optional

from .token_manager import count_tokens
from .query_prompts import SYSTEM_PROMPT, QUERY_MODEL_ID
try:
    from agents.shared.advisory import build_advisory_answer
except ImportError:
    from shared.advisory import build_advisory_answer  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

app = BedrockAgentCoreApp()

region = os.getenv('AWS_REGION', 'us-east-1')
metadata_table_name = os.getenv('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
dynamodb = boto3.resource('dynamodb', region_name=region)
metadata_table = dynamodb.Table(metadata_table_name)


def metrics_table():
    """Return the boto3 Table resource for the governed-metrics catalog.

    Mirrors the metadata-query agent's accessor so advisory answers can enumerate
    a layer's governed metrics (keyed by layer id only — the namespace IS the id).
    """
    return dynamodb.Table(os.environ.get('METRICS_TABLE', 'semantic-layer-metrics'))


_boto_session: Optional[boto3.Session] = None


def get_boto_session() -> boto3.Session:
    global _boto_session
    if _boto_session is None:
        _boto_session = boto3.Session(region_name=os.getenv('AWS_REGION', 'us-east-1'))
    return _boto_session


def get_latest_metadata_item(id: str) -> Optional[dict]:
    """Return metadata item with the highest version for the given id."""
    resp = metadata_table.query(KeyConditionExpression=Key('id').eq(id))
    items = resp.get('Items', [])
    if not items:
        return None

    def _version_num(item: dict) -> int:
        try:
            return int(item.get('version', 'v0').lstrip('v'))
        except ValueError:
            return 0

    return max(items, key=_version_num)


@tool
def retrieve_kb_context(user_query: str) -> str:
    """
    Retrieve schema context from Bedrock Knowledge Base.

    Args:
        user_query: Query to send to the KB (e.g. "list all available tables")

    Returns:
        JSON string with retrieved KB context
    """
    try:
        kb_id = os.getenv('SEMANTIC_RAG_KB_ID')
        if not kb_id:
            raise ValueError("SEMANTIC_RAG_KB_ID environment variable is not set")

        session = get_boto_session()
        client = session.client('bedrock-agent-runtime', region_name=region)
        response = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={'text': user_query},
            retrievalConfiguration={
                'vectorSearchConfiguration': {'numberOfResults': 10}
            }
        )
        docs = response.get('retrievalResults', [])
        context_items = [
            {
                "content": d.get('content', {}).get('text', ''),
                "metadata": d.get('metadata', {}),
                "score": d.get('score', 0),
            }
            for d in docs
        ]
        result = json.dumps({
            "query": user_query,
            "kb_id": kb_id,
            "documents_retrieved": len(context_items),
            "context": context_items,
        }, indent=2)
        logger.info(f"retrieve_kb_context: {len(context_items)} docs, {count_tokens(result)} tokens")
        return result
    except Exception as e:
        logger.error(f"retrieve_kb_context error: {e}")
        return json.dumps({"error": str(e)})


def create_suggestions_agent() -> Agent:
    model = BedrockModel(
        model_id=QUERY_MODEL_ID,
        temperature=0.3,
        max_tokens=2000,
        boto_session=get_boto_session(),
    )
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[retrieve_kb_context],
    )


def _synthesize_advisory(prompt: str) -> str:
    """Run the advisory synthesis prompt through the model and return its text.

    A bare model call (no tools) — the advisory module has already assembled the
    grounded metrics + schema context into ``prompt``, so the model only writes
    prose. Used as the ``synthesize`` callable for ``build_advisory_answer``.

    :param prompt: The fully-formed advisory prompt.
    :returns: The model's text answer (empty string on an unexpected shape).
    """
    model = BedrockModel(
        model_id=QUERY_MODEL_ID,
        temperature=0.3,
        max_tokens=2000,
        boto_session=get_boto_session(),
    )
    # A tool-less Agent is the simplest way to get one prose completion from the
    # same Strands/Bedrock path the suggestions agent already uses.
    agent = Agent(model=model, system_prompt="You are a helpful semantic-layer advisor.")
    response = agent(prompt)
    try:
        return response.message['content'][0]['text']
    except (KeyError, IndexError, TypeError):
        return ''


@app.entrypoint
def invoke(payload: Dict[str, Any], context) -> Dict[str, Any]:
    """
    AgentCore entrypoint.

    Payload (suggestions mode — default):
        {"id": "<ontology_config_id>"}
        → {"suggestions": [{"category": "...", "question": "..."}, ...]}

    Payload (advisory mode — answer a free-form question ABOUT the layer):
        {"id": "<ontology_config_id>", "question": "<str>", "mode": "advisory"}
        → {"answer": "<str>", "metrics": [...], "executed_sql": "", "results": []}

    Advisory mode is selected when a non-empty ``question`` is present OR
    ``mode == "advisory"``. With no question (and mode != advisory) the default
    3-suggestion behavior is preserved unchanged (the homepage starter-chips
    use-case). Advisory NEVER runs SQL (structural — see shared/advisory.py).
    """
    session_id = context.session_id if hasattr(context, "session_id") else str(uuid.uuid4())
    # Attach the new Context returned by set_baggage so "session.id" is actually
    # present on the active context (set_baggage alone does not mutate it).
    if _otel_baggage and _otel_context:
        _otel_context.attach(_otel_baggage.set_baggage("session.id", session_id))
    result_text = ''
    try:
        id = payload.get('id', '')
        if not id:
            return {'error': 'id is required in payload'}

        config = get_latest_metadata_item(id)
        if not config:
            return {'error': f'metadata config not found: {id}'}

        ontology_name = config.get('name', 'this data set')

        # --- Advisory mode: answer a free-form question ABOUT the layer --------
        question = (payload.get('question') or '').strip()
        mode = payload.get('mode', '')
        if question or mode == 'advisory':
            logger.info(f"Advisory answer for ontology id={id} question={question!r}")
            return build_advisory_answer(
                question=question,
                layer_id=id,
                kb_retrieve=retrieve_kb_context,
                metrics_table=metrics_table(),
                synthesize=_synthesize_advisory,
                layer_name=ontology_name,
            )

        # --- Suggestions mode (default): 3 starter questions ------------------
        agent = create_suggestions_agent()
        user_input = (
            f"Generate suggested questions for the semantic layer named '{ontology_name}'. "
            f"Retrieve schema context and produce exactly the JSON output described in your instructions."
        )
        logger.info(f"Invoking suggestions agent for ontology id={id} name={ontology_name}")
        response = agent(user_input)
        result_text = response.message['content'][0]['text']

        # Strip markdown code fences only when they wrap the entire response
        stripped = result_text.strip()
        if stripped.startswith('```') and stripped.endswith('```'):
            stripped = stripped[3:]   # remove opening ```
            if stripped.startswith('json\n'):
                stripped = stripped[5:]
            elif stripped.startswith('json'):
                stripped = stripped[4:]
            # remove trailing ```
            if stripped.endswith('```'):
                stripped = stripped[:-3]
            result_text = stripped.strip()

        parsed = json.loads(result_text)
        # Hard cap to 3 — the prompt asks for exactly 3, but enforce it here so a
        # model overshoot can never surface more than 3 suggestions to the UI.
        if isinstance(parsed, dict) and isinstance(parsed.get('suggestions'), list):
            parsed['suggestions'] = parsed['suggestions'][:3]
        return parsed

    except json.JSONDecodeError as e:
        logger.error(f"Agent returned non-JSON: {e}")
        return {'error': f'Agent response was not valid JSON: {str(e)}', 'raw': result_text}
    except Exception as e:
        logger.error(f"Error in suggestions invoke: {e}")
        return {'error': f'Agent execution failed: {str(e)}'}


if __name__ == '__main__':
    app.run()
