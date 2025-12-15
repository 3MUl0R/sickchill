"""
Anthropic Claude API client wrapper for SickChill AI features.

Handles API communication, error handling, and response parsing.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class APIUsage:
    """Token usage from an API request."""

    input_tokens: int
    output_tokens: int
    model: str

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
        track_cost: bool = True,
        cost_context: Optional[str] = None,
        cost_scope_key: Optional[str] = None,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """
        Send an analysis request to Claude and parse the JSON response.

        Includes retry logic with exponential backoff for transient network errors.

        Args:
            prompt: The main prompt text (with placeholders already filled)
            context: Optional additional context (logged but not sent)
            max_tokens: Maximum tokens in response
            system_prompt: Optional system prompt for additional instructions
            track_cost: Whether to record usage in cost tracker (default: True)
            cost_context: Context for cost tracking ("search" or "postprocess")
            cost_scope_key: Scope key for cost tracking (show ID or file fingerprint)
            use_cache: Whether to check/store in response cache (default: True)

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

        # Check cache first
        request_hash = None
        if use_cache:
            cached_response = self._check_cache(
                prompt, context, system_prompt, max_tokens, cost_context, cost_scope_key
            )
            if cached_response is not None:
                logger.debug("Returning cached AI response")
                return cached_response
            # Generate hash for later caching
            request_hash = self._generate_cache_hash(prompt, context, system_prompt, max_tokens)

        last_error = None
        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(MAX_RETRIES):
            try:
                response, usage = self._make_request(prompt, max_tokens, system_prompt)

                # Track cost if requested
                if track_cost and usage:
                    self._record_cost(usage, cost_context, cost_scope_key)

                # Cache the response
                if use_cache and request_hash:
                    self._store_cache(
                        request_hash, response, cost_context, cost_scope_key
                    )

                return response

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

    def _record_cost(
        self,
        usage: APIUsage,
        context: Optional[str],
        scope_key: Optional[str],
    ) -> None:
        """
        Record API usage in the cost tracker.

        Args:
            usage: The APIUsage from the request
            context: The context ("search" or "postprocess")
            scope_key: The scope key (show ID or file fingerprint)
        """
        try:
            from sickchill.oldbeard.ai.cost_tracker import get_cost_tracker

            tracker = get_cost_tracker()
            tracker.record_usage(
                model=usage.model,
                context=context or "unknown",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                scope_key=scope_key,
            )
        except Exception as e:
            # Don't let cost tracking errors affect the main flow
            logger.debug(f"Failed to record cost: {e}")

    def _generate_cache_hash(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        """
        Generate a hash for caching the request.

        Args:
            prompt: The prompt text
            context: Optional context dict
            system_prompt: Optional system prompt
            max_tokens: Maximum tokens in response

        Returns:
            Hash string for cache key
        """
        import hashlib

        # Build cache key components including model, system_prompt, and max_tokens
        # to ensure different configurations get different cache entries
        cache_parts = [
            prompt,
            self.model,
            str(max_tokens),
        ]

        if context:
            cache_parts.append(json.dumps(context, sort_keys=True))

        if system_prompt:
            cache_parts.append(system_prompt)

        cache_key = "|".join(cache_parts)
        return hashlib.sha256(cache_key.encode()).hexdigest()[:32]

    def _check_cache(
        self,
        prompt: str,
        context: Optional[Dict[str, Any]],
        system_prompt: Optional[str],
        max_tokens: int,
        cost_context: Optional[str],
        scope_key: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Check if a cached response exists for this request.

        Args:
            prompt: The prompt text
            context: Optional context dict
            system_prompt: Optional system prompt
            max_tokens: Maximum tokens in response
            cost_context: The context type
            scope_key: The scope key

        Returns:
            Cached response dict or None
        """
        try:
            from sickchill.oldbeard.ai import get_throttle

            throttle = get_throttle()
            request_hash = self._generate_cache_hash(prompt, context, system_prompt, max_tokens)
            cached = throttle.get_cached_response(request_hash)
            if cached:
                logger.debug(f"Cache hit for {cost_context}/{scope_key}")
                return cached
        except Exception as e:
            logger.debug(f"Cache check failed: {e}")
        return None

    def _store_cache(
        self,
        request_hash: str,
        response: Dict[str, Any],
        cost_context: Optional[str],
        scope_key: Optional[str],
    ) -> None:
        """
        Store a response in the cache.

        Args:
            request_hash: The hash for this request
            response: The response to cache
            cost_context: The context type
            scope_key: The scope key
        """
        try:
            from sickchill.oldbeard.ai import get_throttle

            throttle = get_throttle()
            throttle.cache_response(
                request_hash=request_hash,
                response=response,
                context=cost_context or "unknown",
                scope="show" if cost_context == "search" else "file",
                scope_key=scope_key or "unknown",
            )
            logger.debug(f"Response cached for {cost_context}/{scope_key}")
        except Exception as e:
            logger.debug(f"Failed to cache response: {e}")

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
                logger.debug(
                    f"AI response: {response.usage.input_tokens} input, "
                    f"{response.usage.output_tokens} output tokens"
                )
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
            parsed = json.loads(response_text.strip())
            if isinstance(parsed, dict):
                return parsed
            # Not a dict (e.g., array) - continue to other methods
        except json.JSONDecodeError:
            pass

        # Try to find JSON in code blocks
        code_block_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
        matches = re.findall(code_block_pattern, response_text, re.DOTALL)
        for match in matches:
            try:
                parsed = json.loads(match.strip())
                if isinstance(parsed, dict):
                    return parsed
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
