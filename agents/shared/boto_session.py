"""Shared boto3 session management for credential injection.

A single process-wide boto3 Session used by all query/build agents for their
AWS API calls. The session is injectable (``set_boto_session``) so notebooks
and tests can supply their own credentials; otherwise a region-resolved
default is created lazily on first ``get_boto_session`` call.
"""

import logging
import os
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

# Process-wide boto3 session for credential injection (used in notebooks/testing).
_boto_session: Optional[boto3.Session] = None


def _resolve_region() -> str:
    """Resolve the AWS region from the environment, then boto3, then us-east-1.

    Returns:
        str: The resolved region name (never empty).
    """
    region = os.environ.get("AWS_REGION")
    if not region:
        # Fall back to boto3's own resolution (config/profile), then a hard default.
        region = boto3.Session().region_name or "us-east-1"
    return region


def set_boto_session(session: boto3.Session) -> None:
    """Inject a boto3 Session to use for all AWS API calls.

    Useful for injecting credentials from notebooks or tests. If the provided
    session has no region set, a new session is created that preserves the same
    profile/credentials but includes the resolved region (from AWS_REGION env
    var, defaulting to us-east-1). This ensures downstream boto3 clients (e.g.
    BedrockModel) can always determine the region.

    Args:
        session: Configured boto3.Session with the desired credentials.
    """
    global _boto_session
    region_name = session.region_name or os.getenv("AWS_REGION", "us-east-1")

    if session.region_name is None:
        # boto3 sessions are immutable — rebuild with the resolved region while
        # preserving the profile (accessed via the internal botocore session).
        try:
            profile_name = session._session.get_config_variable("profile") or None
        except Exception:
            profile_name = None
        _boto_session = boto3.Session(
            profile_name=profile_name, region_name=region_name
        )
    else:
        _boto_session = session

    logger.info(f"Boto3 session set with region: {region_name}")


def get_boto_session() -> boto3.Session:
    """Return the active boto3 Session, creating a region-resolved default if unset.

    Returns:
        boto3.Session: The injected session, or a lazily-created default whose
        region is resolved via :func:`_resolve_region`.
    """
    global _boto_session
    if _boto_session is None:
        region = _resolve_region()
        _boto_session = boto3.Session(region_name=region)
        logger.info(f"Created boto3 session with region: {region}")
    return _boto_session
