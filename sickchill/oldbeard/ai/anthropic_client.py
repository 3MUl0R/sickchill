"""
Anthropic Claude API client wrapper for SickChill AI features.

Handles API communication, error handling, and response parsing for the
API-key (BYOK) provider. The provider-agnostic orchestration (caching, cost
tracking, retries, JSON parsing) lives in ``base_client.BaseAIClient``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from sickchill.oldbeard.ai.base_client import (
    BACKOFF_MULTIPLIER,
    INITIAL_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_RETRIES,
    AIConfigurationError,
    AIError,
    AIRateLimitError,
    AIResponseError,
    APIUsage,
    BaseAIClient,
)

logger = logging.getLogger(__name__)

# Re-exported for backwards compatibility: existing imports and tests reference
# these names from this module (e.g. ``from ...anthropic_client import MAX_RETRIES``).
__all__ = [
    "AnthropicClient",
    "APIUsage",
    "AIError",
    "AIConfigurationError",
    "AIRateLimitError",
    "AIResponseError",
    "MAX_RETRIES",
    "INITIAL_BACKOFF_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "BACKOFF_MULTIPLIER",
]


class AnthropicClient(BaseAIClient):
    """
    Wrapper for the Anthropic Claude API.

    Provides methods for sending prompts and parsing responses,
    with proper error handling and logging.
    """

    PROVIDER = "api"

    # Supported models with their display names
    SUPPORTED_MODELS = {
        "claude-sonnet-4-20250514": "Claude Sonnet 4 (Recommended)",
        "claude-haiku-3-5-20241022": "Claude 3.5 Haiku (Faster, cheaper)",
    }

    DEFAULT_MODEL = "claude-sonnet-4-20250514"
    DEFAULT_TIMEOUT = 30
    DEFAULT_MAX_TOKENS = 1024

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """
        Initialize the Anthropic client.

        Args:
            api_key: Anthropic API key
            model: Model identifier (e.g., "claude-sonnet-4-20250514")
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.model = model if model in self.SUPPORTED_MODELS else self.DEFAULT_MODEL
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        """
        Lazily initialize the Anthropic client.
        """
        if self._client is None:
            try:
                import anthropic

                self._client = anthropic.Anthropic(
                    api_key=self.api_key,
                    timeout=self.timeout,
                )
            except ImportError:
                logger.error("anthropic package is not installed. Run: pip install anthropic")
                raise AIConfigurationError("anthropic package is not installed")
            except Exception as e:
                logger.error(f"Failed to initialize Anthropic client: {e}")
                raise AIConfigurationError(f"Failed to initialize Anthropic client: {e}")
        return self._client

    def is_configured(self) -> bool:
        """
        Check if the client is properly configured with an API key.

        Returns:
            True if API key is set (non-empty string)

        Note:
            We only check if the key is non-empty. Actual key validity
            is verified by test_connection() which makes a real API call.
            This avoids false negatives from format changes.
        """
        return bool(self.api_key and self.api_key.strip())

    def test_connection(self) -> Tuple[bool, str]:
        """
        Test the API connection with a minimal request.

        Returns:
            Tuple of (success: bool, message: str)
        """
        if not self.is_configured():
            return False, "API key is not configured"

        try:
            client = self._get_client()

            # Send a minimal test message
            response = client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Say 'OK' if you can read this."}],
            )

            if response.content and len(response.content) > 0:
                return True, f"Connection successful. Model: {self.model}"
            else:
                return False, "API returned empty response"

        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)

            # Parse common error types
            if "authentication" in error_msg.lower() or "api key" in error_msg.lower():
                return False, "Invalid API key"
            elif "rate" in error_msg.lower() and "limit" in error_msg.lower():
                return False, "Rate limit exceeded. Try again later."
            elif "timeout" in error_msg.lower():
                return False, f"Request timed out after {self.timeout}s"
            else:
                logger.error(f"API test failed: {error_type}: {error_msg}")
                return False, f"Connection failed: {error_msg}"

    def _make_request(
        self,
        prompt: str,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> Tuple[Dict[str, Any], Optional[APIUsage]]:
        """
        Make a single API request to Claude.

        Args:
            prompt: The prompt text
            max_tokens: Maximum tokens in response
            system_prompt: Optional system prompt

        Returns:
            Tuple of (parsed JSON response, APIUsage or None)

        Raises:
            AIError subclasses on various error conditions
        """
        try:
            client = self._get_client()

            # Build messages
            messages = [{"role": "user", "content": prompt}]

            # Build request kwargs
            kwargs = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": messages,
            }

            if system_prompt:
                kwargs["system"] = system_prompt

            # Send request
            response = client.messages.create(**kwargs)

            # Extract text content
            if not response.content:
                raise AIResponseError("Empty response from API")

            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            if not response_text:
                raise AIResponseError("No text content in response")

            # Extract usage info
            usage = None
            if hasattr(response, "usage"):
                logger.debug(f"AI response: {response.usage.input_tokens} input, {response.usage.output_tokens} output tokens")
                usage = APIUsage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    model=self.model,
                )

            # Parse JSON from response
            return self._parse_json_response(response_text), usage

        except AIError:
            raise
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)

            # Categorize errors
            if "rate" in error_msg.lower() and "limit" in error_msg.lower():
                raise AIRateLimitError(f"Rate limit exceeded: {error_msg}")
            elif "authentication" in error_msg.lower():
                raise AIConfigurationError(f"Authentication failed: {error_msg}")
            elif self._is_transient_error(e):
                # Transient errors can be retried
                raise AIError(f"Transient error: {error_msg}")
            else:
                logger.error(f"AI request failed: {error_type}: {error_msg}")
                raise AIError(f"API request failed: {error_msg}")
