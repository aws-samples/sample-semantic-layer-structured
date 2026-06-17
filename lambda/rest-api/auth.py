"""
Authentication module for validating JWT tokens from AWS Cognito.
"""

import os
import json
import logging
import requests
import jwt
from jwt.exceptions import InvalidTokenError, ExpiredSignatureError
from fastapi import HTTPException
import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger(__name__)

# Cache for JWKS keys
jwks_cache = {}


def validate_token(token, user_id=None):
    """
    Validate a JWT token from AWS Cognito.

    Args:
        token: JWT token to validate
        user_id: Optional user ID to verify against token claims

    Returns:
        dict: User information extracted from the token

    Raises:
        HTTPException: If token is invalid or expired
    """

    # Continue with normal token validation
    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    try:
        # Get Cognito configuration from environment variables
        user_pool_id = os.environ.get("COGNITO_USER_POOL_ID")
        app_client_id = os.environ.get("COGNITO_APP_CLIENT_ID")
        aws_region = os.environ.get("AWS_REGION", "us-east-1")

        if not user_pool_id or not app_client_id:
            logger.error("Missing Cognito configuration")
            raise HTTPException(
                status_code=500, detail="Authentication service misconfigured"
            )

        # Get the JWKS URL for the user pool
        jwks_url = f"https://cognito-idp.{aws_region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"

        # Fetch JWKS if not in cache
        if jwks_url not in jwks_cache:
            logger.info(f"Fetching JWKS from {jwks_url}")
            # Use 30 second timeout to prevent hanging connections
            response = requests.get(jwks_url, timeout=30)
            response.raise_for_status()
            jwks_cache[jwks_url] = response.json()

        # Get the JWKS
        jwks = jwks_cache[jwks_url]

        # Decode the token header to get the key ID
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        if not kid:
            raise HTTPException(status_code=401, detail="Invalid token header")

        # Find the key with matching key ID
        key = None
        for jwk in jwks.get("keys", []):
            if jwk.get("kid") == kid:
                key = jwk
                break

        if not key:
            raise HTTPException(status_code=401, detail="Token signing key not found")

        # Construct the public key
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))

        # Verify and decode the token
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_signature": True},
            audience=app_client_id,
            issuer=f"https://cognito-idp.{aws_region}.amazonaws.com/{user_pool_id}",
        )

        # Extract user information from token
        token_user_id = payload.get("sub")
        logger.info(f"Token user ID: {token_user_id}")
        email = payload.get("email", "")
        username = payload.get(
            "cognito:username", payload.get("preferred_username", token_user_id)
        )

        # Verify user ID if provided - check against multiple possible fields
        if user_id:
            # The user_id could match the sub, email, or username fields
            valid_user_ids = [token_user_id, email, username]
            if user_id not in valid_user_ids:
                logger.warning(
                    f"User ID mismatch: provided={user_id}, token_sub={token_user_id}"
                )
                raise HTTPException(
                    status_code=403, detail="Token does not match provided user ID"
                )
            else:
                logger.info("User ID matches token claims")
        # Get AWS credentials for the authenticated user
        aws_credentials = get_aws_credentials_for_user(token)

        # Return user information
        return {
            "user_id": token_user_id,
            "email": email,
            "username": username,
            "aws_credentials": aws_credentials,
        }

    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    except Exception as e:
        logger.error(f"Error validating token: {str(e)}")
        raise HTTPException(status_code=500, detail="Authentication service error")


def get_aws_credentials_for_user(token):
    """
    Get AWS credentials for the authenticated user.

    Args:
        token: JWT token from Cognito

    Returns:
        dict: AWS credentials
    """
    try:
        # Get Cognito configuration from environment variables
        identity_pool_id = os.environ.get("COGNITO_IDENTITY_POOL_ID")
        aws_region = os.environ.get("AWS_REGION", "us-east-1")

        if not identity_pool_id:
            logger.warning(
                "Missing Cognito Identity Pool ID, cannot get AWS credentials"
            )
            return None

        # Create a Cognito Identity client
        client = boto3.client("cognito-identity", region_name=aws_region)

        # Get an identity ID for the user
        response = client.get_id(
            IdentityPoolId=identity_pool_id,
            Logins={
                f"cognito-idp.{aws_region}.amazonaws.com/{os.environ.get('COGNITO_USER_POOL_ID')}": token
            },
        )
        identity_id = response["IdentityId"]

        # Get credentials for the identity
        response = client.get_credentials_for_identity(
            IdentityId=identity_id,
            Logins={
                f"cognito-idp.{aws_region}.amazonaws.com/{os.environ.get('COGNITO_USER_POOL_ID')}": token
            },
        )

        # Return the credentials
        return {
            "access_key": response["Credentials"]["AccessKeyId"],
            "secret_key": response["Credentials"]["SecretKey"],
            "session_token": response["Credentials"]["SessionToken"],
        }

    except ClientError as e:
        logger.error(f"Error getting AWS credentials: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error getting AWS credentials: {str(e)}")
        return None
