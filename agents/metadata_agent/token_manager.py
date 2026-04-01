"""
Token Management Utilities
Provides accurate token counting for Claude models using tiktoken
"""

import tiktoken
import logging

logger = logging.getLogger(__name__)

# Token management constants
MAX_TOKENS_PER_REQUEST = 150000  # Conservative limit to avoid hitting max_tokens
MAX_TABLES_PER_BATCH = 3  # Process max 3 tables at once


def count_tokens(text: str) -> int:
    """
    Count tokens in text using tiktoken for accurate token counting.

    Uses cl100k_base encoding which is compatible with GPT-4 and Claude models.
    This provides a good approximation for Claude token counts.

    Args:
        text: Text to analyze for token count

    Returns:
        int: Number of tokens in the text

    Example:
        >>> count_tokens("Hello, world!")
        4
    """
    try:
        # Use cl100k_base encoding which is compatible with GPT-4 and Claude models
        # This provides a good approximation for Claude token counts
        encoding = tiktoken.get_encoding("cl100k_base")
        tokens = encoding.encode(text)
        token_count = len(tokens)

        return token_count
    except Exception as e:
        logger.warning(f"Failed to count tokens with tiktoken: {str(e)}")
        # Fallback: rough estimate (4 chars per token average)
        return len(text) // 4


def check_token_limit(text: str, limit: int = MAX_TOKENS_PER_REQUEST) -> tuple[bool, int]:
    """
    Check if text is within token limit.

    Args:
        text: Text to check
        limit: Maximum allowed tokens (default: MAX_TOKENS_PER_REQUEST)

    Returns:
        tuple: (is_within_limit: bool, token_count: int)

    Example:
        >>> is_safe, count = check_token_limit("Some text")
        >>> if not is_safe:
        ...     print(f"Text exceeds limit: {count} tokens")
    """
    token_count = count_tokens(text)
    is_within_limit = token_count <= limit

    if not is_within_limit:
        logger.warning(f"Token count {token_count} exceeds limit {limit}")

    return is_within_limit, token_count


def get_token_status(token_count: int) -> str:
    """
    Get token usage status indicator.

    Args:
        token_count: Number of tokens

    Returns:
        str: Status indicator (SAFE, WARNING, or DANGER)
    """
    if token_count > 100000:
        return "DANGER"
    elif token_count > 50000:
        return "WARNING"
    else:
        return "SAFE"
