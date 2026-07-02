"""
Retry Utilities for Handling Transient AWS Service Errors

Provides retry decorators with exponential backoff for Bedrock and other AWS services.
Handles 50x errors (503, 500, 502, 504) and throttling (429) gracefully.
Includes Strands Agent lifecycle hooks for seamless integration.
"""

import logging
import asyncio
import time
from functools import wraps
from typing import Callable, TypeVar, Optional
from botocore.exceptions import ClientError, EventStreamError
from botocore.config import Config

logger = logging.getLogger(__name__)

# Import Strands hooks if available
try:
    from strands.hooks import AfterModelInvocationEvent, Hook

    STRANDS_AVAILABLE = True
except ImportError:
    STRANDS_AVAILABLE = False
    Hook = None
    logger.warning("Strands hooks not available - lifecycle hooks will be disabled")

# Type variable for generic function return types
T = TypeVar("T")


class RetryConfig:
    """Configuration for retry behavior"""

    def __init__(
        self,
        max_attempts: int = 3,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
    ):
        self.max_attempts = max_attempts
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.exponential_base = exponential_base
        self.jitter = jitter


# Default configurations for different scenarios
DEFAULT_RETRY_CONFIG = RetryConfig(max_attempts=3, initial_backoff=10.0)
AGGRESSIVE_RETRY_CONFIG = RetryConfig(
    max_attempts=5, initial_backoff=20.0, max_backoff=120.0
)
STREAMING_RETRY_CONFIG = RetryConfig(
    max_attempts=2, initial_backoff=5, max_backoff=20.0
)


# Configure retry behavior for boto3 clients
retry_config = Config(
    retries={
        "max_attempts": AGGRESSIVE_RETRY_CONFIG.max_attempts,
        "mode": "adaptive",  # or 'standard'
    },
    connect_timeout=10,
    read_timeout=240,
)


# Strands Agent Lifecycle Hooks
class BedrockRetryHook:
    """
    Strands Agent lifecycle hook for handling 50x and throttling errors.

    This hook automatically retries on:
    - 50x server errors (500, 502, 503, 504)
    - 429 throttling errors
    - Model stream/timeout errors
    - Connection errors

    Uses exponential backoff with max 60s delay.

    Usage:
        agent = Agent(
            model=model,
            hooks=[BedrockRetryHook()]
        )
    """

    def register_hooks(self, registry):
        """Register the hook with the Strands registry"""
        if STRANDS_AVAILABLE:
            registry.after_model_invocation(self._handle_error)

    def _handle_error(self, event: "AfterModelInvocationEvent"):
        """Handle model invocation errors and configure retry behavior"""
        if not STRANDS_AVAILABLE:
            return

        if not event.error:
            return

        error = event.error
        should_retry = False
        retry_delay = 10.0
        error_code = "Unknown"

        # Handle EventStreamError (common with Bedrock streaming)
        if isinstance(error, EventStreamError):
            error_message = str(error).lower()  # Case-insensitive matching
            error_dict = error.kwargs.get("Error", {})
            error_code = error_dict.get("Code", "Unknown")

            # Service unavailable (503) - check both lowercase and code
            if (
                "serviceunavailable" in error_message
                or error_code == "ServiceUnavailableException"
            ):
                should_retry = True
                error_code = "503_ServiceUnavailable"

            # Throttling (429)
            elif "throttling" in error_message or error_code == "ThrottlingException":
                should_retry = True
                error_code = "429_Throttling"

            # Model stream errors
            elif (
                "modelstreamerror" in error_message
                or error_code == "ModelStreamErrorException"
            ):
                should_retry = True
                error_code = "ModelStreamError"

            # Model timeout errors
            elif (
                "modeltimeout" in error_message or error_code == "ModelTimeoutException"
            ):
                should_retry = True
                error_code = "ModelTimeout"

            # Other 50x server errors
            elif any(
                code.lower() in error_message
                for code in [
                    "InternalServerError",
                    "BadGatewayException",
                    "GatewayTimeoutException",
                ]
            ):
                should_retry = True
                error_code = "5xx_ServerError"

        # Handle standard ClientError
        elif isinstance(error, ClientError):
            error_code = error.response.get("Error", {}).get("Code", "Unknown")
            http_status = error.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode", 0
            )

            # 50x errors - always retry
            if 500 <= http_status < 600:
                should_retry = True
                error_code = f"{http_status}_{error_code}"

            # Throttling (429) - always retry
            elif http_status == 429 or error_code in [
                "ThrottlingException",
                "TooManyRequestsException",
            ]:
                should_retry = True
                error_code = f"429_{error_code}"

            # Specific 4xx errors that should be retried
            elif error_code in ["RequestTimeoutException", "RequestAbortedException"]:
                should_retry = True
                error_code = f"4xx_{error_code}"

        # Handle timeout errors
        elif isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            should_retry = True
            error_code = "Timeout"

        # Handle connection errors
        elif isinstance(error, (ConnectionError, OSError)):
            should_retry = True
            error_code = "ConnectionError"

        # If should retry, configure the event
        if should_retry:
            event.retry = True
            # Exponential backoff: 2^attempt seconds, max 60s
            retry_delay = min(5**event.attempt, 120)
            event.retry_delay = retry_delay

            logger.warning(
                f"Bedrock request failed with retryable error ({error_code}). "
                f"Attempt {event.attempt}. Retrying in {retry_delay}s... "
                f"Error: {error}"
            )
        else:
            logger.error(
                f"Bedrock request failed with non-retryable error ({error_code}): {error}"
            )


# Create a default instance for backward compatibility
bedrock_retry_handler = BedrockRetryHook()


def create_custom_retry_handler(
    max_attempts: int = 5,
    max_backoff: float = 60.0,
    retryable_codes: Optional[list[str]] = None,
):
    """
    Create a custom retry handler with specific configuration.

    Args:
        max_attempts: Maximum number of retry attempts
        max_backoff: Maximum backoff delay in seconds
        retryable_codes: Additional error codes to retry beyond defaults

    Returns:
        Configured retry handler Hook instance

    Usage:
        custom_handler = create_custom_retry_handler(max_attempts=3, max_backoff=30)
        agent = Agent(
            model=model,
            hooks=[custom_handler]
        )
    """
    extra_codes = retryable_codes or []

    class CustomRetryHook:
        """Custom retry hook with configurable parameters"""

        def register_hooks(self, registry):
            """Register the hook with the Strands registry"""
            if STRANDS_AVAILABLE:
                registry.after_model_invocation(self._handle_error)

        def _handle_error(self, event: "AfterModelInvocationEvent"):
            if not STRANDS_AVAILABLE or not event.error:
                return

            # First check if we've exceeded max attempts
            if event.attempt >= max_attempts:
                logger.error(f"Max retry attempts ({max_attempts}) reached. Giving up.")
                return

            # Use the standard handler logic
            base_handler = BedrockRetryHook()
            base_handler._handle_error(event)

            # If not already marked for retry, check custom codes
            if not event.retry and extra_codes:
                error = event.error
                error_str = str(error)

                if any(code in error_str for code in extra_codes):
                    event.retry = True
                    event.retry_delay = min(2**event.attempt, max_backoff)
                    logger.warning(
                        f"Retrying on custom error code. Attempt {event.attempt}. "
                        f"Delay: {event.retry_delay}s"
                    )

            # Enforce max backoff
            if event.retry and event.retry_delay:
                event.retry_delay = min(event.retry_delay, max_backoff)

    return CustomRetryHook()


def _is_retryable_error(error: Exception) -> tuple[bool, Optional[str]]:
    """
    Determine if an error is retryable and return the reason.

    Returns:
        Tuple of (is_retryable, error_code)
    """
    # Handle Bedrock EventStreamError
    if isinstance(error, EventStreamError):
        error_code = error.kwargs.get("Error", {}).get("Code", "Unknown")
        error_message = str(error).lower()  # Case-insensitive matching

        # Service unavailable errors (503) - always retry
        if (
            "serviceunavailable" in error_message
            or error_code == "ServiceUnavailableException"
        ):
            return True, "503_ServiceUnavailable"

        # Throttling errors (429) - always retry
        if "throttling" in error_message or error_code == "ThrottlingException":
            return True, "429_Throttling"

        # Model stream errors - retry
        if (
            "modelstreamerror" in error_message
            or error_code == "ModelStreamErrorException"
        ):
            return True, "ModelStreamError"

        # Model timeout errors - retry
        if "modeltimeout" in error_message or error_code == "ModelTimeoutException":
            return True, "ModelTimeout"

        # Generic service errors (500, 502, 504) - retry
        if any(
            code.lower() in error_message
            for code in [
                "InternalServerError",
                "BadGatewayException",
                "GatewayTimeoutException",
            ]
        ):
            return True, "5xx_ServerError"

        # Client errors (4xx) - generally don't retry except for throttling
        # ValidationException, AccessDeniedException, etc. should not be retried
        if any(
            code in error_message
            for code in [
                "ValidationException",
                "AccessDeniedException",
                "ResourceNotFoundException",
            ]
        ):
            return False, f"4xx_{error_code}"

        # Unknown EventStreamError - don't retry by default
        logger.warning(f"Unknown EventStreamError: {error_message}")
        return False, "Unknown_EventStreamError"

    # Handle standard boto3 ClientError
    if isinstance(error, ClientError):
        error_code = error.response.get("Error", {}).get("Code", "Unknown")
        http_status = error.response.get("ResponseMetadata", {}).get(
            "HTTPStatusCode", 0
        )

        # 5xx errors - always retry
        if 500 <= http_status < 600:
            return True, f"{http_status}_{error_code}"

        # Throttling (429) - always retry
        if http_status == 429 or error_code in [
            "ThrottlingException",
            "TooManyRequestsException",
        ]:
            return True, f"429_{error_code}"

        # 4xx errors - generally don't retry
        return False, f"{http_status}_{error_code}"

    # Handle network/timeout errors - retry
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return True, "Timeout"

    # Handle connection errors - retry
    if isinstance(error, (ConnectionError, OSError)):
        return True, "ConnectionError"

    # Handle Strands EventLoopException - check the underlying error
    if error.__class__.__name__ == "EventLoopException":
        # Try to get the original exception
        if hasattr(error, "__cause__") and error.__cause__:
            return _is_retryable_error(error.__cause__)
        # Check the error message
        error_message = str(error)
        if "serviceUnavailableException" in error_message:
            return True, "503_ServiceUnavailable"
        if "ThrottlingException" in error_message:
            return True, "429_Throttling"

    # Unknown error - don't retry by default
    return False, "Unknown"


def _calculate_backoff(attempt: int, config: RetryConfig) -> float:
    """Calculate backoff time with exponential backoff and optional jitter"""
    backoff = min(
        config.initial_backoff * (config.exponential_base ** (attempt - 1)),
        config.max_backoff,
    )

    # Add jitter to prevent thundering herd
    # Using random for timing jitter is acceptable (not cryptographic)
    if config.jitter:
        import random  # nosec B311

        backoff = backoff * (0.5 + random.random() * 0.5)  # nosec B311 - 50-100% of calculated backoff

    return backoff


def with_retry(config: Optional[RetryConfig] = None):
    """
    Decorator to add retry logic with exponential backoff to async functions.

    Usage:
        @with_retry(DEFAULT_RETRY_CONFIG)
        async def my_function():
            # function that might fail
            pass
    """
    if config is None:
        config = DEFAULT_RETRY_CONFIG

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_error = None

            for attempt in range(1, config.max_attempts + 1):
                try:
                    return await func(*args, **kwargs)

                except Exception as e:
                    last_error = e
                    is_retryable, error_code = _is_retryable_error(e)

                    if not is_retryable:
                        logger.warning(
                            f"{func.__name__} failed with non-retryable error ({error_code}): {e}",
                            exc_info=True
                        )
                        raise

                    if attempt >= config.max_attempts:
                        logger.warning(
                            f"{func.__name__} failed after {config.max_attempts} attempts. "
                            f"Last error ({error_code}): {e}",
                            exc_info=True
                        )
                        raise

                    backoff = _calculate_backoff(attempt, config)
                    logger.warning(
                        f"{func.__name__} attempt {attempt}/{config.max_attempts} failed "
                        f"with retryable error ({error_code}): {e}. "
                        f"Retrying in {backoff:.2f}s..."
                    )

                    await asyncio.sleep(backoff)

            # This should never be reached, but just in case
            raise last_error

        return wrapper

    return decorator


def with_sync_retry(config: Optional[RetryConfig] = None):
    """
    Decorator to add retry logic with exponential backoff to synchronous functions.

    Usage:
        @with_sync_retry(DEFAULT_RETRY_CONFIG)
        def my_function():
            # function that might fail
            pass
    """
    if config is None:
        config = DEFAULT_RETRY_CONFIG

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_error = None

            for attempt in range(1, config.max_attempts + 1):
                try:
                    return func(*args, **kwargs)

                except Exception as e:
                    last_error = e
                    is_retryable, error_code = _is_retryable_error(e)

                    if not is_retryable:
                        logger.warning(
                            f"{func.__name__} failed with non-retryable error ({error_code}): {e}",
                            exc_info=True
                        )
                        raise

                    if attempt >= config.max_attempts:
                        logger.warning(
                            f"{func.__name__} failed after {config.max_attempts} attempts. "
                            f"Last error ({error_code}): {e}",
                            exc_info=True
                        )
                        raise

                    backoff = _calculate_backoff(attempt, config)
                    logger.warning(
                        f"{func.__name__} attempt {attempt}/{config.max_attempts} failed "
                        f"with retryable error ({error_code}): {e}. "
                        f"Retrying in {backoff:.2f}s..."
                    )

                    time.sleep(backoff)

            # This should never be reached, but just in case
            raise last_error

        return wrapper

    return decorator


# Circuit breaker pattern (optional advanced feature)
class CircuitBreaker:
    """
    Circuit breaker to prevent repeated calls to failing services.

    States:
    - CLOSED: Normal operation
    - OPEN: Too many failures, reject requests immediately
    - HALF_OPEN: Testing if service has recovered
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exception: type = Exception,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.state = "CLOSED"

    def call(self, func: Callable, *args, **kwargs):
        """Execute function with circuit breaker protection"""
        if self.state == "OPEN":
            if time.time() - self.last_failure_time >= self.recovery_timeout:
                logger.info("Circuit breaker entering HALF_OPEN state")
                self.state = "HALF_OPEN"
            else:
                raise Exception(
                    f"Circuit breaker is OPEN. Service unavailable for {self.recovery_timeout}s."
                )

        try:
            result = func(*args, **kwargs)

            # Success - reset circuit breaker
            if self.state == "HALF_OPEN":
                logger.info("Circuit breaker recovered, entering CLOSED state")
                self.state = "CLOSED"
                self.failure_count = 0

            return result

        except self.expected_exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.failure_count >= self.failure_threshold:
                logger.error(
                    f"Circuit breaker opening after {self.failure_count} failures"
                )
                self.state = "OPEN"

            raise


# Example usage patterns
if __name__ == "__main__":
    # Example 1: Basic retry with default config
    @with_retry()
    async def example_bedrock_call():
        # Your Bedrock API call here
        raise NotImplementedError("Replace with actual implementation")

    # Example 2: Aggressive retry for critical operations
    @with_retry(AGGRESSIVE_RETRY_CONFIG)
    async def critical_operation():
        # Your critical operation here
        raise NotImplementedError("Replace with actual implementation")

    # Example 3: Streaming with shorter timeouts
    @with_retry(STREAMING_RETRY_CONFIG)
    async def streaming_operation():
        # Your streaming operation here
        raise NotImplementedError("Replace with actual implementation")
