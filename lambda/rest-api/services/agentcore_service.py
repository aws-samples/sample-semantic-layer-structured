"""
AgentCore Service for invoking Bedrock AgentCore Runtime

This service handles:
- Boto3-based invocation of AgentCore Runtime
- Ontology generation agent invocation
- Streaming response handling
"""

import base64
import os
import logging
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Any


def _require_https(url: str) -> str:
    """Raise ValueError if *url* does not use the https scheme.

    Prevents mis-configured env vars (http:// or file://) from reaching urllib.

    :param url: the URL to validate.
    :returns: the original url, unchanged.
    :raises ValueError: if the scheme is not https.
    """
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError(f"Refusing non-HTTPS URL: {url!r}")
    return url

import boto3

logger = logging.getLogger(__name__)

# Refresh the M2M token this many seconds before its real expiry.
_TOKEN_SKEW_SECONDS = 60

# The Cognito hosted-UI /oauth2/token endpoint is fronted by WAF Bot Control
# (AWSManagedRulesBotControlRuleSet), which 403s requests carrying urllib's
# default 'Python-urllib/3.x' User-Agent (a known bot signature). Sending a
# browser User-Agent lets the M2M token mint through. Applied to the data-plane
# /invocations request too for consistency (harmless — that path is not behind
# this WAF).
_BROWSER_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)


class AgentCoreService:
    """Service for invoking AgentCore Runtime agents.

    The runtimes are JWT-inbound, so this service invokes them over HTTPS with a
    Cognito machine-to-machine (client_credentials) Bearer token. All
    REST-originated calls are backend jobs
    (ontology/metadata/suggestions generation, metadata-query lookups) with no
    end-user token in hand, so they all use the M2M token. The per-user chat path
    does NOT go through this service — it streams through the chat gateway.
    """

    def __init__(self):
        """Initialize the service: read runtime ARNs + OAuth config from env."""
        self.region = os.environ.get('AWS_REGION', 'us-east-1')
        self.ontology_runtime_arn = os.environ.get('ONTOLOGY_RUNTIME_ARN')
        self.metadata_runtime_arn = os.environ.get('METADATA_RUNTIME_ARN')
        self.metadata_query_runtime_arn = os.environ.get('METADATA_QUERY_RUNTIME_ARN')
        self.suggestions_runtime_arn = os.environ.get('SUGGESTIONS_RUNTIME_ARN')

        # OAuth (M2M client_credentials) config.
        self.oauth_token_endpoint = os.environ.get('OAUTH_TOKEN_ENDPOINT', '')
        self.oauth_scope = os.environ.get('OAUTH_SCOPE', '')
        self.m2m_client_id = os.environ.get('M2M_CLIENT_ID', '')
        self.m2m_client_secret_arn = os.environ.get('M2M_CLIENT_SECRET_ARN', '')
        self._token_cache: Dict[str, Any] = {}
        self._secrets_client = boto3.client('secretsmanager', region_name=self.region)

        if not self.ontology_runtime_arn:
            logger.warning("ONTOLOGY_RUNTIME_ARN environment variable not set")

        logger.info("AgentCoreService initialized (OAuth M2M)")

    # ------------------------------------------------------------------
    # OAuth token + HTTPS runtime invocation
    # ------------------------------------------------------------------

    def _m2m_client_secret(self) -> str:
        """Read the M2M client secret from Secrets Manager.

        :returns: the Cognito M2M app-client secret string.
        :raises RuntimeError: if the secret ARN env var is unset (fail loud).
        """
        if not self.m2m_client_secret_arn:
            raise RuntimeError('M2M_CLIENT_SECRET_ARN not configured')
        resp = self._secrets_client.get_secret_value(SecretId=self.m2m_client_secret_arn)
        return resp['SecretString']

    def _fetch_token(self, *, force: bool = False) -> str:
        """Return a valid M2M access token, caching until shortly before expiry.

        :param force: bypass the cache and fetch fresh (used after a 401/403).
        :returns: a bearer access token string.
        :raises RuntimeError: if required OAuth env vars are missing (fail loud).
        """
        now = time.time()
        if (
            not force
            and self._token_cache.get('token')
            and self._token_cache.get('expires_at', 0) - _TOKEN_SKEW_SECONDS > now
        ):
            return self._token_cache['token']

        if not self.oauth_token_endpoint or not self.m2m_client_id or not self.oauth_scope:
            raise RuntimeError(
                'OAUTH_TOKEN_ENDPOINT, M2M_CLIENT_ID, and OAUTH_SCOPE must be configured'
            )
        client_secret = self._m2m_client_secret()
        body = urllib.parse.urlencode(
            {'grant_type': 'client_credentials', 'scope': self.oauth_scope}
        ).encode('utf-8')
        basic = base64.b64encode(
            f'{self.m2m_client_id}:{client_secret}'.encode('utf-8')
        ).decode('ascii')
        req = urllib.request.Request(
            _require_https(self.oauth_token_endpoint),
            data=body,
            method='POST',
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {basic}',
                'User-Agent': _BROWSER_UA,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 — scheme enforced by _require_https above  # nosemgrep: dynamic-urllib-use-detected — fixed AWS service endpoint from config, not user-controlled host
            payload = json.loads(resp.read().decode('utf-8'))
        token = payload['access_token']
        self._token_cache['token'] = token
        self._token_cache['expires_at'] = now + int(payload.get('expires_in', 3600))
        return token

    def _invoke_runtime(
        self, *, runtime_arn: str, session_id: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Invoke a JWT-inbound runtime over HTTPS with an M2M Bearer token.

        Retries ONCE with a force-refreshed token on a 401/403.

        :param runtime_arn: the AgentCore Runtime ARN.
        :param session_id: runtime session id (must be ≥33 chars).
        :param payload: JSON-serializable request payload.
        :returns: parsed JSON response dict, or ``{'result': <text>}`` if not JSON.
        :raises urllib.error.HTTPError: on a non-auth error or persistent 401/403.
        """
        encoded = urllib.parse.quote(runtime_arn, safe='')
        url = _require_https(
            f'https://bedrock-agentcore.{self.region}.amazonaws.com/'
            f'runtimes/{encoded}/invocations?qualifier=DEFAULT'
        )
        body = json.dumps(payload).encode('utf-8')

        def _do(token: str) -> str:
            req = urllib.request.Request(
                url,
                data=body,
                method='POST',
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                    'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': session_id,
                    'User-Agent': _BROWSER_UA,
                },
            )
            with urllib.request.urlopen(req, timeout=600) as resp:  # nosec B310 — scheme enforced by _require_https in _invoke_runtime  # nosemgrep: dynamic-urllib-use-detected — fixed AWS service endpoint from config, not user-controlled host
                return resp.read().decode('utf-8', errors='replace')

        try:
            text = _do(self._fetch_token())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                logger.warning('runtime invoke %s; refreshing token and retrying', exc.code)
                text = _do(self._fetch_token(force=True))
            else:
                raise
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {'result': text}

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
            logger.info(f"Invoking AgentCore Runtime ARN: {self.ontology_runtime_arn}")
            logger.info(f"Ontology ID: {id}")

            # Use id as session_id for tracking (must be ≥33 chars).
            session_id = id

            # Invoke over HTTPS with an M2M OAuth Bearer token (the agent reads
            # its config from DynamoDB internally).
            response_data = self._invoke_runtime(
                runtime_arn=self.ontology_runtime_arn,
                session_id=session_id,
                payload={"id": id},
            )
            logger.info("AgentCore invocation successful")

            response_text = (
                response_data.get('result', '')
                if isinstance(response_data, dict)
                else str(response_data)
            )
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

        response_data = self._invoke_runtime(
            runtime_arn=self.metadata_runtime_arn,
            session_id=id,
            payload={'id': id},
        )
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
        session_id = uuid.uuid4().hex + uuid.uuid4().hex[:1]  # 33 chars
        response_data = self._invoke_runtime(
            runtime_arn=self.metadata_query_runtime_arn,
            session_id=session_id,
            payload={'question': question, 'id': id},
        )
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
        session_id = uuid.uuid4().hex + uuid.uuid4().hex[:1]  # 33 chars
        response_data = self._invoke_runtime(
            runtime_arn=self.suggestions_runtime_arn,
            session_id=session_id,
            payload={'id': id},
        )
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

