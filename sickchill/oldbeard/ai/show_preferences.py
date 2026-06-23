"""
Per-Show AI Preferences for SickChill.

Allows users to configure AI behavior on a per-show basis,
overriding global settings for specific shows.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

from sickchill import settings
from sickchill.oldbeard import db

if TYPE_CHECKING:
    from sickchill.tv import TVShow

logger = logging.getLogger(__name__)


@dataclass
class ShowAIPreferences:
    """AI preferences for a specific show."""

    # Whether AI is enabled for this show (None = use global setting)
    ai_enabled: Optional[bool] = None

    # Whether to always use AI for search (override AI_SEARCH_ONLY_ON_FAILURE)
    ai_search_always: bool = False

    # Whether to skip AI search entirely for this show
    ai_search_disabled: bool = False

    # Whether to always use AI for post-processing
    ai_postprocess_always: bool = False

    # Whether to skip AI post-processing entirely for this show
    ai_postprocess_disabled: bool = False

    # Custom confidence threshold for this show (None = use global)
    ai_confidence_threshold: Optional[float] = None

    # Custom cooldown days for this show (None = use global)
    ai_search_cooldown_days: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ShowAIPreferences":
        """Create from dictionary."""
        return cls(
            ai_enabled=data.get("ai_enabled"),
            ai_search_always=data.get("ai_search_always", False),
            ai_search_disabled=data.get("ai_search_disabled", False),
            ai_postprocess_always=data.get("ai_postprocess_always", False),
            ai_postprocess_disabled=data.get("ai_postprocess_disabled", False),
            ai_confidence_threshold=data.get("ai_confidence_threshold"),
            ai_search_cooldown_days=data.get("ai_search_cooldown_days"),
        )


class ShowPreferencesManager:
    """
    Manages per-show AI preferences.

    Stores preferences in cache.db for persistence.
    """

    def __init__(self):
        """Initialize the preferences manager."""
        self._ensure_table()
        # In-memory cache for frequently accessed preferences
        self._cache: Dict[int, ShowAIPreferences] = {}

    def _get_db(self) -> db.DBConnection:
        """Get a database connection to cache.db."""
        return db.DBConnection("cache.db")

    def _ensure_table(self) -> None:
        """Ensure the preferences table exists."""
        cache_db = self._get_db()

        if not cache_db.has_table("ai_show_preferences"):
            logger.info("Creating ai_show_preferences table")
            cache_db.action(
                """
                CREATE TABLE ai_show_preferences (
                    show_id INTEGER PRIMARY KEY,
                    preferences_json TEXT NOT NULL
                )
                """
            )

    def get_preferences(self, show_id: int) -> ShowAIPreferences:
        """
        Get AI preferences for a show.

        Args:
            show_id: The show's indexer ID

        Returns:
            ShowAIPreferences object (defaults if not configured)
        """
        # Check cache first
        if show_id in self._cache:
            return self._cache[show_id]

        # Load from database
        cache_db = self._get_db()
        result = cache_db.select(
            "SELECT preferences_json FROM ai_show_preferences WHERE show_id = ?",
            [show_id],
        )

        if result:
            try:
                data = json.loads(result[0]["preferences_json"])
                prefs = ShowAIPreferences.from_dict(data)
                self._cache[show_id] = prefs
                return prefs
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Invalid preferences for show {show_id}: {e}")

        # Return defaults
        prefs = ShowAIPreferences()
        self._cache[show_id] = prefs
        return prefs

    def set_preferences(self, show_id: int, preferences: ShowAIPreferences) -> None:
        """
        Set AI preferences for a show.

        Args:
            show_id: The show's indexer ID
            preferences: The preferences to set
        """
        cache_db = self._get_db()
        prefs_json = json.dumps(preferences.to_dict())

        cache_db.action(
            """
            INSERT OR REPLACE INTO ai_show_preferences (show_id, preferences_json)
            VALUES (?, ?)
            """,
            [show_id, prefs_json],
        )

        # Update cache
        self._cache[show_id] = preferences

        logger.debug(f"Set AI preferences for show {show_id}")

    def update_preference(self, show_id: int, key: str, value: Any) -> None:
        """
        Update a single preference for a show.

        Args:
            show_id: The show's indexer ID
            key: The preference key to update
            value: The new value
        """
        prefs = self.get_preferences(show_id)
        if hasattr(prefs, key):
            setattr(prefs, key, value)
            self.set_preferences(show_id, prefs)
        else:
            logger.warning(f"Unknown preference key: {key}")

    def delete_preferences(self, show_id: int) -> None:
        """
        Delete AI preferences for a show (revert to defaults).

        Args:
            show_id: The show's indexer ID
        """
        cache_db = self._get_db()
        cache_db.action(
            "DELETE FROM ai_show_preferences WHERE show_id = ?",
            [show_id],
        )

        # Remove from cache
        self._cache.pop(show_id, None)

        logger.debug(f"Deleted AI preferences for show {show_id}")

    def clear_cache(self) -> None:
        """Clear the in-memory cache."""
        self._cache.clear()

    def is_ai_enabled_for_show(self, show: "TVShow") -> bool:
        """
        Check if AI is enabled for a specific show.

        Combines global settings with per-show preferences.

        Args:
            show: The TVShow object

        Returns:
            True if AI should be enabled for this show
        """
        if not settings.AI_ENABLED:
            return False

        prefs = self.get_preferences(show.indexerid)

        # Per-show override takes precedence
        if prefs.ai_enabled is not None:
            return prefs.ai_enabled

        # Fall back to global setting
        return True

    def should_use_ai_search(
        self,
        show: "TVShow",
        has_result: bool,
    ) -> bool:
        """
        Determine if AI search should be used for a show.

        Args:
            show: The TVShow object
            has_result: Whether rule-based picker found a result

        Returns:
            True if AI search should be attempted
        """
        if not self.is_ai_enabled_for_show(show):
            return False

        if not settings.AI_SEARCH_ENABLED:
            return False

        prefs = self.get_preferences(show.indexerid)

        # Check if AI search is disabled for this show
        if prefs.ai_search_disabled:
            return False

        # Check if AI should always be used for this show
        if prefs.ai_search_always:
            return True

        # Use global setting for "only on failure"
        if settings.AI_SEARCH_ONLY_ON_FAILURE and has_result:
            return False

        return True

    def should_use_ai_postprocess(self, show: "TVShow", has_match: bool) -> bool:
        """
        Determine if AI post-processing should be used for a show.

        Args:
            show: The TVShow object
            has_match: Whether normal parsing found a match

        Returns:
            True if AI post-processing should be attempted
        """
        if not self.is_ai_enabled_for_show(show):
            return False

        if not settings.AI_POSTPROCESS_MATCH_ENABLED:
            return False

        prefs = self.get_preferences(show.indexerid)

        # Check if AI post-process is disabled for this show
        if prefs.ai_postprocess_disabled:
            return False

        # Check if AI should always be used for this show
        if prefs.ai_postprocess_always:
            return True

        # Use global setting for "only on failure"
        if settings.AI_POSTPROCESS_MATCH_ONLY_ON_FAILURE and has_match:
            return False

        return True

    def get_confidence_threshold(self, show: "TVShow") -> float:
        """
        Get the confidence threshold for a show.

        Args:
            show: The TVShow object

        Returns:
            Confidence threshold (per-show or global)
        """
        prefs = self.get_preferences(show.indexerid)
        if prefs.ai_confidence_threshold is not None:
            return prefs.ai_confidence_threshold
        return settings.AI_CONFIDENCE_THRESHOLD

    def get_search_cooldown_days(self, show: "TVShow") -> int:
        """
        Get the search cooldown days for a show.

        Args:
            show: The TVShow object

        Returns:
            Cooldown days (per-show or global)
        """
        prefs = self.get_preferences(show.indexerid)
        if prefs.ai_search_cooldown_days is not None:
            return prefs.ai_search_cooldown_days
        return settings.AI_SEARCH_COOLDOWN_DAYS_PER_SHOW


# Module-level singleton
_preferences_manager: Optional[ShowPreferencesManager] = None


def get_preferences_manager() -> ShowPreferencesManager:
    """Get the global ShowPreferencesManager instance."""
    global _preferences_manager
    if _preferences_manager is None:
        _preferences_manager = ShowPreferencesManager()
    return _preferences_manager
