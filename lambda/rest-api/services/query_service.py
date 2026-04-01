"""
Query Service for Natural Language Query Processing

Architecture — fully async with S3 result storage:

  submit_query()
    → stores {status: SUBMITTED} in DynamoDB (QUERY_RESULTS_TABLE)
    → self-invokes Lambda async (_worker event)
    → returns {queryId, status: SUBMITTED} immediately (< 1 second)

  process_worker_event()
    → called by the async self-invocation
    → runs AgentCore (37-60 seconds)
    → parses structured JSON response from agent
    → stores full result in S3: query-results/<queryId>/result.json
    → updates DynamoDB: {status: COMPLETED, result_s3_key, answer_preview}

  get_query_status()
    → reads status from DynamoDB only (fast, no S3 call)

  get_query_result()
    → reads DynamoDB item to get result_s3_key
    → fetches full structured result from S3
    → returns {answer, results[], reasoning{}, n_quads[]}

DynamoDB table (QUERY_RESULTS_TABLE, PK: queryId, TTL: expires):
  Tracks: status, result_s3_key, answer_preview, error, timestamps

S3 (ARTIFACTS_BUCKET, key: query-results/<queryId>/result.json):
  Stores: full structured result — answer, results[], reasoning{}, n_quads[]
  Avoids DynamoDB 400 KB item limit for large query results.
"""

import os
import json
import logging
import uuid
import boto3
from services.guardrail_service import GuardrailService
from botocore.config import Config
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_RESULT_S3_PREFIX = "query-results"
_TTL_DAYS = 7


class QueryService:

    def __init__(self):
        self.region = os.environ.get('AWS_REGION', 'us-east-1')
        self.query_runtime_arn = os.environ.get('QUERY_RUNTIME_ARN')
        self.metadata_query_runtime_arn = os.environ.get('METADATA_QUERY_RUNTIME_ARN')
        self.table_name = os.environ.get('QUERY_RESULTS_TABLE', 'semantic-layer-query-results')
        self.ontology_table_name = os.environ.get('ONTOLOGY_METADATA_TABLE', 'semantic-layer-metadata')
        self.artifacts_bucket = os.environ.get('ARTIFACTS_BUCKET', '')
        self._table = None
        self.guardrail = GuardrailService()

        if not self.query_runtime_arn:
            logger.warning("QUERY_RUNTIME_ARN not set")
        if not self.metadata_query_runtime_arn:
            logger.warning("METADATA_QUERY_RUNTIME_ARN not set")
        if not self.artifacts_bucket:
            logger.warning("ARTIFACTS_BUCKET not set — S3 result storage will fail")

    # -------------------------------------------------------------------------
    # DynamoDB helpers (status tracking only — no result payload)
    # -------------------------------------------------------------------------

    def _get_table(self):
        if self._table is None:
            self._table = boto3.resource(
                'dynamodb', region_name=self.region
            ).Table(self.table_name)
        return self._table

    def _put_query(self, query_id: str, item: dict):
        self._get_table().put_item(Item={'queryId': query_id, **item})

    def _get_query_item(self, query_id: str) -> Optional[dict]:
        resp = self._get_table().get_item(Key={'queryId': query_id})
        return resp.get('Item')

    def _update_query(self, query_id: str, updates: dict):
        set_expr = ', '.join(f'#{k} = :{k}' for k in updates)
        names = {f'#{k}': k for k in updates}
        values = {f':{k}': v for k, v in updates.items()}
        self._get_table().update_item(
            Key={'queryId': query_id},
            UpdateExpression=f'SET {set_expr}',
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    @staticmethod
    def _ttl_epoch() -> int:
        """Return Unix epoch seconds for TTL_DAYS from now."""
        return int((datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)).timestamp())

    # -------------------------------------------------------------------------
    # S3 helpers (full result payload)
    # -------------------------------------------------------------------------

    def _result_s3_key(self, query_id: str) -> str:
        return f"{_RESULT_S3_PREFIX}/{query_id}/result.json"

    def _write_result_to_s3(self, query_id: str, result: dict) -> str:
        """Serialize result to JSON and upload to S3.  Returns the S3 key."""
        s3 = boto3.client('s3', region_name=self.region)
        key = self._result_s3_key(query_id)
        s3.put_object(
            Bucket=self.artifacts_bucket,
            Key=key,
            Body=json.dumps(result, ensure_ascii=False).encode('utf-8'),
            ContentType='application/json',
        )
        logger.info(f"Result written to s3://{self.artifacts_bucket}/{key}")
        return key

    def _read_result_from_s3(self, s3_key: str) -> dict:
        """Fetch and deserialize a result JSON from S3."""
        s3 = boto3.client('s3', region_name=self.region)
        obj = s3.get_object(Bucket=self.artifacts_bucket, Key=s3_key)
        return json.loads(obj['Body'].read().decode('utf-8'))

    # -------------------------------------------------------------------------
    # Ontology config helpers
    # -------------------------------------------------------------------------

    def _lookup_ontology_type(self, id: str) -> str:
        """
        Read the ontology config from DynamoDB and return its type ('VKG' or 'SemanticRAG').
        Falls back to 'VKG' if the item is not found or the type is missing.
        """
        try:
            table = boto3.resource('dynamodb', region_name=self.region).Table(self.ontology_table_name)
            resp = table.get_item(Key={'id': id, 'version': 'v1'})
            return resp.get('Item', {}).get('type', 'VKG')
        except Exception as e:
            logger.warning(f"Could not look up ontology type for id={id}: {e} — defaulting to VKG")
            return 'VKG'

    def _runtime_arn_for_type(self, ontology_type: str) -> str:
        """Return the AgentCore runtime ARN for the given ontology type."""
        if ontology_type == 'SemanticRAG':
            if not self.metadata_query_runtime_arn:
                raise ValueError("METADATA_QUERY_RUNTIME_ARN is not configured")
            return self.metadata_query_runtime_arn
        if not self.query_runtime_arn:
            raise ValueError("QUERY_RUNTIME_ARN is not configured")
        return self.query_runtime_arn

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def submit_query(
        self,
        question: str,
        id: str,
    ) -> Dict[str, Any]:
        """
        Accept a query and return immediately.
        Processing happens asynchronously via Lambda self-invocation.
        """
        if not self.query_runtime_arn and not self.metadata_query_runtime_arn:
            raise ValueError("Neither QUERY_RUNTIME_ARN nor METADATA_QUERY_RUNTIME_ARN is configured")

        query_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        self._put_query(query_id, {
            'status': 'SUBMITTED',
            'question': question,
            'id': id,
            'created_at': now,
            'updated_at': now,
            'expires': self._ttl_epoch(),
        })

        function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
        if function_name:
            boto3.client('lambda', region_name=self.region).invoke(
                FunctionName=function_name,
                InvocationType='Event',
                Payload=json.dumps({
                    '_worker': True,
                    'query_id': query_id,
                    'question': question,
                    'id': id,
                }).encode(),
            )
            logger.info(f"Async worker invoked for query {query_id}")
        else:
            logger.error("AWS_LAMBDA_FUNCTION_NAME not set — worker cannot be invoked")

        return {
            'queryId': query_id,
            'status': 'SUBMITTED',
            'message': 'Query accepted. Poll /query/status/{queryId} for results.',
        }

    def process_worker_event(self, event: dict):
        """
        Called by the async self-invocation.
        Runs the AgentCore call, parses structured JSON, stores in S3, updates DynamoDB.
        """
        query_id = event['query_id']
        question = event['question']
        id = event['id']

        logger.info(f"Worker started for query {query_id}")
        now = datetime.now(timezone.utc).isoformat()
        self._update_query(query_id, {'status': 'RUNNING', 'updated_at': now})

        try:
            # --- INPUT guardrail pre-screen ---
            input_check = self.guardrail.apply(question, source='INPUT')
            if input_check['blocked']:
                logger.warning(f"Query {query_id} blocked by input guardrail")
                now2 = datetime.now(timezone.utc).isoformat()
                self._update_query(query_id, {
                    'status': 'BLOCKED',
                    'error': input_check['message'],
                    'updated_at': now2,
                })
                return

            ontology_type = self._lookup_ontology_type(id)
            runtime_arn = self._runtime_arn_for_type(ontology_type)
            logger.info(f"Query {query_id}: ontology_type={ontology_type} runtime={runtime_arn}")

            agentcore_client = boto3.client(
                'bedrock-agentcore',
                region_name=self.region,
                config=Config(read_timeout=600, connect_timeout=10),
            )

            logger.info(f"Invoking AgentCore for query {query_id}")
            response = agentcore_client.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=query_id,
                payload=json.dumps({"question": question, "id": id}).encode('utf-8'),
                qualifier='DEFAULT',
            )

            chunks = [chunk.decode('utf-8') for chunk in response.get('response', [])]
            response_text = ''.join(chunks)
            logger.info(f"AgentCore response: {len(response_text)} chars for query {query_id}")

            # Parse structured JSON from agent response
            result = self._parse_agent_response(response_text)

            # --- OUTPUT guardrail post-screen ---
            answer_text = result.get('answer', '')
            if answer_text:
                output_check = self.guardrail.apply(answer_text, source='OUTPUT')
                if output_check['blocked']:
                    logger.warning(f"Query {query_id} answer blocked by output guardrail")
                    result['answer'] = output_check['message']
                    result['results'] = []
                    result['sql_query'] = ''

            # Handle clarification requests — store status without writing to S3
            if result.get('needs_clarification'):
                now2 = datetime.now(timezone.utc).isoformat()
                self._update_query(query_id, {
                    'status': 'NEEDS_CLARIFICATION',
                    'clarification_question': result['clarification_question'],
                    'clarification_options': json.dumps(result['options']),
                    'updated_at': now2,
                })
                logger.info(f"Query {query_id} needs clarification")
                return  # do not write to S3, do not mark COMPLETED

            # Store full result in S3 (avoids DynamoDB 400 KB item limit)
            s3_key = self._write_result_to_s3(query_id, result)

            # DynamoDB stores only tracking metadata — no raw payload
            answer_preview = (result.get('answer') or '')[:500]
            now2 = datetime.now(timezone.utc).isoformat()
            self._update_query(query_id, {
                'status': 'COMPLETED',
                'result_s3_key': s3_key,
                'answer_preview': answer_preview,
                'updated_at': now2,
            })
            logger.info(f"Query {query_id} completed — result at s3://{self.artifacts_bucket}/{s3_key}")

        except Exception as e:
            logger.error(f"Worker error for query {query_id}: {e}", exc_info=True)
            now2 = datetime.now(timezone.utc).isoformat()
            self._update_query(query_id, {
                'status': 'FAILED',
                'error': str(e),
                'updated_at': now2,
            })

    def get_query_status(self, query_id: str) -> Dict[str, Any]:
        """Fast status check — reads DynamoDB only, no S3 call."""
        item = self._get_query_item(query_id)
        if not item:
            return {'status': 'NOT_FOUND'}
        return {
            'query_id': query_id,
            'status': item['status'],
            'answer_preview': item.get('answer_preview', ''),
            'created_at': item.get('created_at'),
            'updated_at': item.get('updated_at'),
        }

    def get_query_result(self, query_id: str) -> Dict[str, Any]:
        """
        Return the full structured result.
        Fetches from S3 when COMPLETED; returns error info when FAILED.
        """
        item = self._get_query_item(query_id)
        if not item:
            raise ValueError(f"Query not found: {query_id}")

        base = {
            'query_id': query_id,
            'status': item['status'],
            'created_at': item.get('created_at'),
            'updated_at': item.get('updated_at'),
        }

        if item['status'] == 'COMPLETED':
            s3_key = item.get('result_s3_key')
            if s3_key:
                full_result = self._read_result_from_s3(s3_key)
                return {**base, **full_result}
            # Fallback if S3 key missing (shouldn't happen)
            return {**base, 'answer': item.get('answer_preview', ''), 'results': [], 'reasoning': None}

        if item['status'] == 'NEEDS_CLARIFICATION':
            return {
                **base,
                'needs_clarification': True,
                'clarification_question': item.get('clarification_question', ''),
                'options': json.loads(item.get('clarification_options', '[]')),
                'answer': '', 'results': [], 'reasoning': None,
            }

        if item['status'] == 'FAILED':
            return {**base, 'error': item.get('error', 'Unknown error'), 'answer': '', 'results': [], 'reasoning': None}

        if item['status'] == 'BLOCKED':
            return {**base, 'blocked': True, 'error': item.get('error', 'Content blocked by safety policy.'), 'answer': '', 'results': [], 'reasoning': None}

        # SUBMITTED or RUNNING — no result yet
        return {**base, 'answer': '', 'results': [], 'reasoning': None}

    # -------------------------------------------------------------------------
    # Response parsing
    # -------------------------------------------------------------------------

    def _parse_agent_response(self, response_text: str) -> dict:
        """
        Parse the agent's response.

        The agent's invoke() entrypoint returns a structured dict with all fields
        already populated from tool-call caches (sql_query, results, n_quads,
        reasoning) and a plain-English 'answer' string.  This method normalises
        that dict and handles edge cases gracefully.

        Priority order for the 'answer' field:
          1. data['answer'] — plain English sentence from the LLM's final text
          2. If data['answer'] is itself a JSON string (old behaviour), extract
             the nested 'answer' key from it
          3. Fall back to the raw response text if JSON parsing fails entirely
        """
        text = response_text.strip()

        # Strip accidental markdown code fences
        if text.startswith('```'):
            lines = text.split('\n')
            text = '\n'.join(
                line for line in lines
                if not line.strip().startswith('```')
            ).strip()

        try:
            data = json.loads(text)

            if data.get('needs_clarification'):
                return {
                    'answer': '',
                    'sql_query': '',
                    'results': [],
                    'n_quads': [],
                    'reasoning': {},
                    'needs_clarification': True,
                    'clarification_question': data.get('clarification_question', 'Please clarify your question.'),
                    'options': data.get('options', []),
                }

            # Extract the answer — guard against nested JSON in the answer field
            raw_answer = data.get('answer') or data.get('result') or ''
            if isinstance(raw_answer, str):
                trimmed = raw_answer.strip()
                if trimmed.startswith('{') or trimmed.startswith('['):
                    try:
                        inner = json.loads(trimmed)
                        if isinstance(inner, dict):
                            raw_answer = (
                                inner.get('answer')
                                or inner.get('result')
                                or raw_answer
                            )
                    except (json.JSONDecodeError, ValueError):
                        pass  # Not JSON — use as-is

            return {
                'answer': raw_answer,
                'sql_query': data.get('sql_query') or data.get('sqlQuery') or '',
                'results': self._normalise_results(data.get('results', [])),
                'n_quads': data.get('n_quads') or data.get('nQuads') or [],
                'reasoning': data.get('reasoning') or {},
            }
        except (json.JSONDecodeError, ValueError):
            logger.warning("Agent response is not JSON — storing as plain answer")
            return {
                'answer': response_text,
                'sql_query': '',
                'results': [],
                'n_quads': [],
                'reasoning': {},
            }

    @staticmethod
    def _normalise_results(raw: Any) -> List[dict]:
        """Ensure results is a list of dicts (rows)."""
        if not raw:
            return []
        if isinstance(raw, list) and all(isinstance(r, dict) for r in raw):
            return raw
        # Wrap unexpected formats gracefully
        return [{'value': str(r)} for r in (raw if isinstance(raw, list) else [raw])]

