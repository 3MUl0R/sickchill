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
    from sickchill.oldbeard.ai.batch import BatchProcessor
    from sickchill.oldbeard.ai.cost_tracker import CostTracker
    from sickchill.oldbeard.ai.feedback import FeedbackManager
    from sickchill.oldbeard.ai.show_preferences import ShowPreferencesManager
    from sickchill.oldbeard.ai.throttle import ThrottleManager

__all__ = [
    "get_client",
    "get_throttle",
    "get_cost_tracker",
    "get_preferences_manager",
    "get_feedback_manager",
    "get_batch_processor",
    "reset_client",
    "is_ai_available",
]

# Module-level singleton instances for client and throttle (core AI functionality)
# These don't have module-level getters in their respective modules
_client: Optional["AnthropicClient"] = None
_throttle: Optional["ThrottleManager"] = None


def get_client() -> Optional["AnthropicClient"]:
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


def get_throttle() -> "ThrottleManager":
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


def get_cost_tracker() -> "CostTracker":
    """
    Get the singleton CostTracker instance.
    Delegates to the module-level singleton in cost_tracker.py.
    """
    from sickchill.oldbeard.ai.cost_tracker import get_cost_tracker as _get_ct

    return _get_ct()


def get_preferences_manager() -> "ShowPreferencesManager":
    """
    Get the singleton ShowPreferencesManager instance.
    Delegates to the module-level singleton in show_preferences.py.
    """
    from sickchill.oldbeard.ai.show_preferences import (
        get_preferences_manager as _get_pm,
    )

    return _get_pm()


def get_feedback_manager() -> "FeedbackManager":
    """
    Get the singleton FeedbackManager instance.
    Delegates to the module-level singleton in feedback.py.
    """
    from sickchill.oldbeard.ai.feedback import get_feedback_manager as _get_fm

    return _get_fm()


def get_batch_processor() -> "BatchProcessor":
    """
    Get the singleton BatchProcessor instance.
    Delegates to the module-level singleton in batch.py.
    """
    from sickchill.oldbeard.ai.batch import get_batch_processor as _get_bp

    return _get_bp()


def is_ai_available() -> bool:
    """
    Check if AI features are available and properly configured.
    """
    from sickchill import settings

    if not settings.AI_ENABLED:
        return False

    client = get_client()
    return client is not None and client.is_configured()
