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
except ImportError:
    _otel_baggage = None  # type: ignore
from strands import Agent, tool
from strands.models import BedrockModel
from boto3.dynamodb.conditions import Key
from typing import Dict, Any, Optional

from .token_manager import count_tokens
from .query_prompts import SYSTEM_PROMPT, QUERY_MODEL_ID

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


@app.entrypoint
def invoke(payload: Dict[str, Any], context) -> Dict[str, Any]:
    """
    AgentCore entrypoint.

    Payload: {"id": "<ontology_config_id>"}
    Returns: {"suggestions": [{"category": "...", "question": "..."}, ...]}
    """
    session_id = context.session_id if hasattr(context, "session_id") else str(uuid.uuid4())
    if _otel_baggage:
        _otel_baggage.set_baggage("session.id", session_id)
    result_text = ''
    try:
        id = payload.get('id', '')
        if not id:
            return {'error': 'id is required in payload'}

        config = get_latest_metadata_item(id)
        if not config:
            return {'error': f'metadata config not found: {id}'}

        agent = create_suggestions_agent()
        ontology_name = config.get('name', 'this data set')
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
        return parsed

    except json.JSONDecodeError as e:
        logger.error(f"Agent returned non-JSON: {e}")
        return {'error': f'Agent response was not valid JSON: {str(e)}', 'raw': result_text}
    except Exception as e:
        logger.error(f"Error in suggestions invoke: {e}")
        return {'error': f'Agent execution failed: {str(e)}'}


if __name__ == '__main__':
    app.run()
