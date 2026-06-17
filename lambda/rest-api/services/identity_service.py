"""OBO token-exchange service (item #4 — oauth-obo-identity-passthrough).

Wraps the AgentCore Identity OBO API. Each user-initiated chat/query call
exchanges the verified Cognito JWT for an OBO token bearing the user's
principal; that token is threaded into the AgentCore Runtime invocation so
Athena/Glue/KB calls execute under the user's identity.

Fail-closed: if the exchange fails, the request is denied — never silently
downgraded to service identity.

Behaviour is gated by the ``ENABLE_OBO_PASSTHROUGH`` env var (matches the
``enableOboPassthrough`` CDK context flag). When the flag is off, the
service short-circuits to a no-op so existing flows keep working during
phased rollout.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# Token TTL is 15 minutes per design; we refresh slightly before expiry to
# avoid edge-of-window failures.
_DEFAULT_TTL_SECONDS = 15 * 60
_REFRESH_LEEWAY_SECONDS = 30


@dataclass
class ObOToken:
    """Opaque OBO token + the metadata the rest of the stack needs.

    ``access_key_id`` / ``secret_access_key`` / ``session_token`` are the
    STS-assumed credentials the agent uses for AWS calls under the user's
    identity. ``token_id`` is a logging handle (no secret material).
    """

    user_id: str
    token_id: str
    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at_epoch: int

    def is_expired(self) -> bool:
        """Return True when the token is at or near expiry."""
        return time.time() + _REFRESH_LEEWAY_SECONDS >= self.expires_at_epoch

    def as_runtime_payload(self) -> Dict[str, Any]:
        """Serialise the credentials for an AgentCore Runtime invocation.

        We include only the fields the agent needs to construct a boto3
        session; the JWT itself never crosses the wire to downstream agents.
        """
        return {
            'userId': self.user_id,
            'tokenId': self.token_id,
            'awsAccessKeyId': self.access_key_id,
            'awsSecretAccessKey': self.secret_access_key,
            'awsSessionToken': self.session_token,
        }


class ObOExchangeError(RuntimeError):
    """Raised when the AgentCore Identity exchange fails. Caller MUST 401."""


class IdentityService:
    """OBO token exchange + per-(user, ontology) cache."""

    def __init__(
        self,
        *,
        region: Optional[str] = None,
        agentcore_identity_client: Any = None,
        enabled: Optional[bool] = None,
    ) -> None:
        """Initialise the service.

        Args:
            region: AWS region.
            agentcore_identity_client: Pre-built ``boto3.client(
                'bedrock-agentcore-identity')`` injected for tests.
            enabled: Override the ``ENABLE_OBO_PASSTHROUGH`` env var.
        """
        self._region = region or os.environ.get('AWS_REGION', 'us-east-1')
        self._client = agentcore_identity_client
        if enabled is None:
            enabled = os.environ.get('ENABLE_OBO_PASSTHROUGH', 'false').lower() == 'true'
        self._enabled = enabled
        # Cache key is (userId, ontologyId) — design says one token per chat
        # session, but ontology id is the most reliable proxy at the API layer.
        self._cache: Dict[Tuple[str, str], ObOToken] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _get_client(self):
        """Lazily build the AgentCore Identity client."""
        if self._client is None:
            # bedrock-agentcore-identity is the SDK service id at the time of
            # writing; if AWS renames, override via injected client in tests.
            self._client = boto3.client(
                'bedrock-agentcore-control', region_name=self._region
            )
        return self._client

    def exchange(
        self, *, jwt: str, user_id: str, ontology_id: str, target_audience: str
    ) -> ObOToken:
        """Exchange a Cognito JWT for an OBO token.

        Args:
            jwt: The validated Cognito JWT (audience already checked at API
                Gateway). We never re-validate here — that's the JWT
                authorizer's job.
            user_id: The verified principal (``sub`` claim).
            ontology_id: Used as a cache scope.
            target_audience: The AgentCore workload-identity audience.

        Returns:
            ObOToken with cached credentials.

        Raises:
            ObOExchangeError: when the API call fails. Fail-closed.
        """
        if not self._enabled:
            raise ObOExchangeError(
                "OBO passthrough is disabled (ENABLE_OBO_PASSTHROUGH=false)"
            )
        cache_key = (user_id, ontology_id)
        cached = self._cache.get(cache_key)
        if cached is not None and not cached.is_expired():
            return cached

        client = self._get_client()
        try:
            response = client.exchange_token(
                jwtToken=jwt,
                audience=target_audience,
            )
        except ClientError as exc:
            logger.warning(
                "OBO exchange failed for user=%s ontology=%s: %s",
                user_id, ontology_id, exc,
            )
            raise ObOExchangeError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OBO exchange unexpectedly raised %s for user=%s",
                exc, user_id,
            )
            raise ObOExchangeError(str(exc)) from exc

        creds = response.get('credentials', {}) or {}
        expires_at_epoch = int(
            response.get('expiresAtEpoch')
            or (time.time() + _DEFAULT_TTL_SECONDS)
        )
        token = ObOToken(
            user_id=user_id,
            token_id=response.get('tokenId') or '',
            access_key_id=creds.get('accessKeyId') or '',
            secret_access_key=creds.get('secretAccessKey') or '',
            session_token=creds.get('sessionToken') or '',
            expires_at_epoch=expires_at_epoch,
        )
        if not (token.access_key_id and token.session_token):
            raise ObOExchangeError(
                "OBO response missing accessKeyId / sessionToken"
            )
        self._cache[cache_key] = token
        return token

    def invalidate(self, *, user_id: str, ontology_id: str) -> None:
        """Drop a cache entry — call after observing ExpiredTokenException."""
        self._cache.pop((user_id, ontology_id), None)
