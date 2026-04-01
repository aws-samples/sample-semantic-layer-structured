"""
AgentCore Service for invoking Bedrock AgentCore Runtime

This service handles:
- Boto3-based invocation of AgentCore Runtime
- Ontology generation agent invocation
- Streaming response handling
"""

import os
import logging
import json
import boto3
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class AgentCoreService:
    """Service for invoking AgentCore Runtime agents"""

    def __init__(self):
        """Initialize AgentCore service with credentials and configuration"""
        self.session = boto3.Session()
        self.credentials = self.session.get_credentials()
        self.region = os.environ.get('AWS_REGION', 'us-east-1')
        self.ontology_runtime_arn = os.environ.get('ONTOLOGY_RUNTIME_ARN')
        self.metadata_runtime_arn = os.environ.get('METADATA_RUNTIME_ARN')
        self.metadata_query_runtime_arn = os.environ.get('METADATA_QUERY_RUNTIME_ARN')
        self.suggestions_runtime_arn = os.environ.get('SUGGESTIONS_RUNTIME_ARN')

        if not self.ontology_runtime_arn:
            logger.warning("ONTOLOGY_RUNTIME_ARN environment variable not set")

        logger.info("AgentCoreService initialized")

    def invoke_ontology_agent(
        self,
        id: str
    ) -> Dict[str, Any]:
        """
        Invoke the Ontology Generation Agent on AgentCore Runtime

        The agent will:
        1. Read ontology config from DynamoDB using id
        2. Build system/user prompts from config
        3. Process tables asynchronously in background
        4. Update DynamoDB with progress
        5. Return immediately (~3s)

        Args:
            id: Unique identifier for the ontology (used as session_id)

        Returns:
            Dictionary containing the agent response

        Raises:
            ValueError: If ONTOLOGY_RUNTIME_ARN is not configured
            Exception: If the invocation fails
        """
        if not self.ontology_runtime_arn:
            raise ValueError("ONTOLOGY_RUNTIME_ARN environment variable is not configured")

        try:
            # Create AgentCore client
            agentcore_client = boto3.client('bedrock-agentcore', region_name=self.region)

            logger.info(f"Invoking AgentCore Runtime ARN: {self.ontology_runtime_arn}")
            logger.info(f"Ontology ID: {id}")

            # Use id as session_id for tracking
            session_id = id

            # Create request payload with just id
            # Agent will read DynamoDB and build prompts internally
            payload_dict = {"id": id}
            payload_json = json.dumps(payload_dict).encode('utf-8')

            # Invoke the agent runtime using boto3
            response = agentcore_client.invoke_agent_runtime(
                agentRuntimeArn=self.ontology_runtime_arn,
                runtimeSessionId=session_id,
                payload=payload_json,
                qualifier='DEFAULT'
            )

            logger.info("AgentCore invocation successful")

            # Read streaming response
            content = []
            for chunk in response.get('response', []):
                content.append(chunk.decode('utf-8'))

            # Join and parse response
            response_text = ''.join(content)
            logger.info(f"Response received: {len(response_text)} characters")

            # Try to parse as JSON, fallback to text
            try:
                response_data = json.loads(response_text)
            except json.JSONDecodeError:
                logger.warning("Response is not JSON, returning as text")
                response_data = {"result": response_text}

            return {
                'success': True,
                'data': response_data,
                'sessionId': session_id,
                'output': response_data.get('result', response_text)
            }

        except Exception as e:
            # Catch all exceptions including botocore.exceptions.ClientError
            logger.warning(f"Error invoking AgentCore: {e}", exc_info=True)
            raise Exception(f"Failed to invoke AgentCore: {str(e)}")

    def invoke_metadata_agent(self, id: str) -> Dict[str, Any]:
        """
        Invoke the Metadata Agent on AgentCore Runtime

        The agent reads its full config (dataSources, descriptions,
        enrichmentAnnotations) from DynamoDB using id — matching the
        ontology agent's pattern exactly.

        Args:
            id: Unique identifier for the job (used as session_id); must be ≥33 chars

        Returns:
            Dictionary containing the agent response

        Raises:
            ValueError: If METADATA_RUNTIME_ARN is not configured
        """
        if not self.metadata_runtime_arn:
            raise ValueError("METADATA_RUNTIME_ARN not configured")

        payload: Dict[str, Any] = {'id': id}

        agentcore_client = boto3.client('bedrock-agentcore', region_name=self.region)
        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=self.metadata_runtime_arn,
            runtimeSessionId=id,
            payload=json.dumps(payload).encode('utf-8'),
            qualifier='DEFAULT',
        )
        content = [chunk.decode('utf-8') for chunk in response.get('response', [])]
        response_text = ''.join(content)
        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError:
            response_data = {'result': response_text}
        return {'success': True, 'data': response_data, 'sessionId': id}

    def invoke_metadata_query_agent(
        self, question: str, id: str
    ) -> Dict[str, Any]:
        """
        Invoke the Metadata Query Agent on AgentCore Runtime

        Args:
            question: Natural language question about metadata
            id: Metadata config ID (used by the agent to look up config in DynamoDB)

        Returns:
            Dictionary containing the agent response

        Raises:
            ValueError: If METADATA_QUERY_RUNTIME_ARN is not configured
        """
        if not self.metadata_query_runtime_arn:
            raise ValueError("METADATA_QUERY_RUNTIME_ARN not configured")

        import uuid
        session_id = str(uuid.uuid4())  # must be ≥33 chars for runtimeSessionId
        payload = {'question': question, 'id': id}

        agentcore_client = boto3.client('bedrock-agentcore', region_name=self.region)
        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=self.metadata_query_runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode('utf-8'),
            qualifier='DEFAULT',
        )
        content = [chunk.decode('utf-8') for chunk in response.get('response', [])]
        response_text = ''.join(content)
        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError:
            response_data = {'result': response_text}
        return {'success': True, 'data': response_data, 'sessionId': session_id}

    def invoke_suggestions_agent(self, id: str) -> Dict[str, Any]:
        """
        Invoke the Query Suggestions Agent on AgentCore Runtime.

        Args:
            id: Ontology config ID — agent reads KB config from DynamoDB

        Returns:
            Dict with 'success', 'data' (containing 'suggestions' list), 'sessionId'

        Raises:
            ValueError: If SUGGESTIONS_RUNTIME_ARN is not configured
        """
        if not self.suggestions_runtime_arn:
            raise ValueError("SUGGESTIONS_RUNTIME_ARN environment variable is not configured")

        import uuid
        session_id = str(uuid.uuid4())
        payload = {'id': id}

        agentcore_client = boto3.client('bedrock-agentcore', region_name=self.region)
        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=self.suggestions_runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode('utf-8'),
            qualifier='DEFAULT',
        )
        content = [chunk.decode('utf-8') for chunk in response.get('response', [])]
        response_text = ''.join(content)
        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError:
            response_data = {'result': response_text}
        return {'success': True, 'data': response_data, 'sessionId': session_id}

    def invoke_query_agent(self) -> Dict[str, Any]:
        """
        Invoke the Semantic Query Agent on AgentCore Runtime

        Returns:
            Dictionary containing the agent response
        """
        # TODO: Implement when query agent is needed
        # Similar structure to invoke_ontology_agent but with query runtime ARN
        raise NotImplementedError("Query agent invocation not yet implemented")
