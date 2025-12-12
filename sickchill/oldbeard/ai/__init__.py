"""
AI module for SickChill - Intelligent Search & Post-Processing

This module provides optional AI-powered intelligence for:
1. Search fallback - When rule-based selection fails
2. Post-processing fallback - When file identification fails
3. File analysis - Quality verification and issue detection (optional)

AI is only used as a fallback when existing logic fails, with throttling
to control costs and prevent repeated attempts on stuck items.

Requires: User-provided Anthropic API key (BYOK model)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sickchill.oldbeard.ai.anthropic_client import AnthropicClient
    from sickchill.oldbeard.ai.throttle import ThrottleManager

__all__ = [
    "get_client",
    "get_throttle",
    "reset_client",
    "is_ai_available",
]

# Module-level singleton instances (initialized lazily)
_client: Optional[AnthropicClient] = None
_throttle: Optional[ThrottleManager] = None


def get_client() -> Optional[AnthropicClient]:
    """
    Get the singleton AnthropicClient instance.
    Returns None if AI is not configured.
    """
    global _client
    if _client is None:
        from sickchill import settings
        from sickchill.oldbeard.ai.anthropic_client import AnthropicClient

        if settings.AI_ENABLED and settings.ANTHROPIC_API_KEY:
            _client = AnthropicClient(
                api_key=settings.ANTHROPIC_API_KEY,
                model=settings.ANTHROPIC_MODEL,
                timeout=settings.AI_REQUEST_TIMEOUT,
            )
    return _client


def get_throttle() -> ThrottleManager:
    """
    Get the singleton ThrottleManager instance.
    """
    global _throttle
    if _throttle is None:
        from sickchill.oldbeard.ai.throttle import ThrottleManager

        _throttle = ThrottleManager()
    return _throttle


def reset_client() -> None:
    """
    Reset the client instance (e.g., after settings change).
    """
    global _client
    _client = None


def is_ai_available() -> bool:
    """
    Check if AI features are available and properly configured.
    """
    from sickchill import settings

    if not settings.AI_ENABLED:
        return False

    client = get_client()
    return client is not None and client.is_configured()
