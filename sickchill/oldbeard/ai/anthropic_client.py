"""
Anthropic Claude API client wrapper for SickChill AI features.

Handles API communication, error handling, and response parsing.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 10.0
BACKOFF_MULTIPLIER = 2.0


class AIError(Exception):
    """Base exception for AI-related errors."""

    pass


class AIConfigurationError(AIError):
    """Raised when AI is not properly configured."""

    pass


class AIRateLimitError(AIError):
    """Raised when API rate limit is exceeded."""

    pass


class AIResponseError(AIError):
    """Raised when API returns an unexpected response."""

    pass


class AnthropicClient:
    """
    Wrapper for the Anthropic Claude API.

    Provides methods for sending prompts and parsing responses,
    with proper error handling and logging.
    """

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

    def analyze(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        system_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an analysis request to Claude and parse the JSON response.

        Includes retry logic with exponential backoff for transient network errors.

        Args:
            prompt: The main prompt text (with placeholders already filled)
            context: Optional additional context (logged but not sent)
            max_tokens: Maximum tokens in response
            system_prompt: Optional system prompt for additional instructions

        Returns:
            Parsed JSON response as a dictionary

        Raises:
            AIConfigurationError: If client is not properly configured
            AIRateLimitError: If rate limit is exceeded
            AIResponseError: If response cannot be parsed
            AIError: For other API errors
        """
        if not self.is_configured():
            raise AIConfigurationError("AI client is not properly configured")

        # Log the request (without sensitive data)
        context_summary = f" (context: {list(context.keys())})" if context else ""
        logger.debug(f"AI request: {len(prompt)} chars{context_summary}")

        last_error = None
        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(MAX_RETRIES):
            try:
                return self._make_request(prompt, max_tokens, system_prompt)

            except AIConfigurationError:
                # Don't retry auth errors
                raise
            except AIRateLimitError:
                # Don't retry rate limit errors (let caller handle cooldown)
                raise
            except AIResponseError:
                # Don't retry response parsing errors (response was received)
                raise
            except AIError as e:
                # Retry transient errors (network issues, timeouts)
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    logger.warning(
                        f"AI request failed (attempt {attempt + 1}/{MAX_RETRIES}), "
                        f"retrying in {backoff:.1f}s: {e}"
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SECONDS)
                else:
                    logger.error(f"AI request failed after {MAX_RETRIES} attempts: {e}")

        # All retries exhausted
        raise last_error or AIError("Request failed after all retries")

    def _make_request(
        self,
        prompt: str,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> Dict[str, Any]:
        """
        Make a single API request to Claude.

        Args:
            prompt: The prompt text
            max_tokens: Maximum tokens in response
            system_prompt: Optional system prompt

        Returns:
            Parsed JSON response

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

            # Log usage
            if hasattr(response, "usage"):
                logger.debug(
                    f"AI response: {response.usage.input_tokens} input, "
                    f"{response.usage.output_tokens} output tokens"
                )

            # Parse JSON from response
            return self._parse_json_response(response_text)

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

    @staticmethod
    def _is_transient_error(error: Exception) -> bool:
        """
        Check if an error is transient and should be retried.

        Args:
            error: The exception to check

        Returns:
            True if the error is likely transient (network issues, timeouts)
        """
        error_msg = str(error).lower()
        transient_indicators = [
            "timeout",
            "timed out",
            "connection",
            "network",
            "temporary",
            "unavailable",
            "502",
            "503",
            "504",
            "overloaded",
        ]
        return any(indicator in error_msg for indicator in transient_indicators)

    def _parse_json_response(self, response_text: str) -> Dict[str, Any]:
        """
        Extract and parse JSON from Claude's response.

        Claude may include text before/after the JSON, so we need to find it.

        Args:
            response_text: Raw response text from Claude

        Returns:
            Parsed JSON as dictionary

        Raises:
            AIResponseError: If JSON cannot be found or parsed
        """
        # First, try to parse the entire response as JSON
        try:
            return json.loads(response_text.strip())
        except json.JSONDecodeError:
            pass

        # Try to find JSON in code blocks
        code_block_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
        matches = re.findall(code_block_pattern, response_text, re.DOTALL)
        for match in matches:
            try:
                return json.loads(match.strip())
            except json.JSONDecodeError:
                continue

        # Try to find JSON object pattern
        json_pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
        matches = re.findall(json_pattern, response_text, re.DOTALL)
        for match in matches:
            try:
                return json.loads(match)
            except json.JSONDecodeError:
                continue

        # Log the problematic response for debugging
        logger.warning(f"Could not parse JSON from response: {response_text[:500]}...")
        raise AIResponseError(
            "Could not parse JSON from AI response. "
            "The model may have returned an unexpected format."
        )
