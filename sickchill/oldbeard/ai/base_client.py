"""
Provider-agnostic base class for SickChill AI clients.

Holds the orchestration that is identical regardless of *how* a prompt reaches
Claude: response caching, cost tracking, retry/backoff, and JSON parsing.
Concrete providers (Anthropic SDK, Claude Code CLI) subclass this and implement
``_make_request``, ``test_connection`` and ``is_configured``.
"""

from __future__ import annotations

import hashlib
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


class BaseAIClient:
    """
    Shared orchestration for AI providers.

    Subclasses MUST set ``self.model`` and implement ``_make_request``,
    ``test_connection`` and ``is_configured``. Everything else (caching,
    cost tracking, retries, JSON parsing) is provided here and is identical
    across providers.
    """

    # Identifies the provider for cache-key namespacing (overridden by subclasses)
    # so an API-key response and a CLI response for an otherwise-identical request
    # never collide in the shared response cache.
    PROVIDER = "base"

    DEFAULT_MAX_TOKENS = 1024

    model: str

    # ------------------------------------------------------------------ #
    # Provider-specific hooks (must be implemented by subclasses)
    # ------------------------------------------------------------------ #
    def is_configured(self) -> bool:
        """Return True if this client can make requests."""
        raise NotImplementedError

    def test_connection(self) -> Tuple[bool, str]:
        """Test connectivity/auth. Returns (success, message)."""
        raise NotImplementedError

    def _make_request(
        self,
        prompt: str,
        max_tokens: int,
        system_prompt: Optional[str],
    ) -> Tuple[Dict[str, Any], Optional[APIUsage]]:
        """Make a single request and return (parsed JSON response, APIUsage or None)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Provider-agnostic orchestration
    # ------------------------------------------------------------------ #
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
        return_meta: bool = False,
    ) -> Any:
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
            return_meta: When True, return ``(response, was_cached)`` so the caller can avoid
                billing a free cache hit against the call budget. When False (default), return
                just the response dict (backwards-compatible).

        Returns:
            The parsed JSON response dict, or ``(response, was_cached)`` if ``return_meta``.

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
            cached_response = self._check_cache(prompt, context, system_prompt, max_tokens, cost_context, cost_scope_key)
            if cached_response is not None:
                logger.debug("Returning cached AI response")
                return (cached_response, True) if return_meta else cached_response
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
                    self._store_cache(request_hash, response, cost_context, cost_scope_key)

                return (response, False) if return_meta else response

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
                    logger.warning(f"AI request failed (attempt {attempt + 1}/{MAX_RETRIES}), retrying in {backoff:.1f}s: {e}")
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
        # Build cache key components including provider, model, system_prompt, and
        # max_tokens to ensure different configurations get different cache entries
        cache_parts = [
            self.PROVIDER,
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
        raise AIResponseError("Could not parse JSON from AI response. The model may have returned an unexpected format.")
