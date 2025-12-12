"""
AI request throttling and rate limiting for SickChill.

Implements:
- Per-show cooldowns for search fallback
- Per-file cooldowns for post-processing fallback
- Global hourly/daily budget limits
- Request caching to avoid duplicate API calls

Note on budget limits:
    The hourly/daily call counters are stored in-memory and reset on restart.
    This is intentional for simplicity - the cooldown/throttle tables in cache.db
    provide persistent rate limiting per-show and per-file. The in-memory budget
    is a soft limit to prevent runaway costs in a single session.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from sickchill import settings
from sickchill.oldbeard import db

logger = logging.getLogger(__name__)


class ThrottleManager:
    """
    Manages rate limiting and cooldowns for AI requests.

    Uses cache.db to persist throttle state across restarts.
    Thread-safe for concurrent access from search/post-processing threads.
    """

    # Context types
    CONTEXT_SEARCH = "search"
    CONTEXT_POSTPROCESS = "postprocess"

    # Scope types
    SCOPE_SHOW = "show"
    SCOPE_FILE = "file"

    def __init__(self):
        """Initialize the throttle manager."""
        self._ensure_tables()
        # In-memory counters for hourly/daily budgets (reset on restart)
        # These are soft limits - see module docstring
        self._hourly_calls: List[float] = []
        self._daily_calls: List[float] = []
        # Lock for thread-safe access to budget counters
        self._budget_lock = threading.Lock()
        # Track in-flight reservations to prevent race conditions
        # Key: scope_key (show ID), Value: timestamp when reserved
        self._pending_reservations: Dict[str, float] = {}

    def _get_db(self) -> db.DBConnection:
        """Get a database connection to cache.db."""
        return db.DBConnection("cache.db")

    def _ensure_tables(self) -> None:
        """
        Ensure AI throttle and cache tables exist.
        This is called on init as a safety measure, but tables should be
        created by the cache.py migration.
        """
        cache_db = self._get_db()

        # Check if tables exist, create if not (fallback for dev/testing)
        if not cache_db.has_table("ai_throttle"):
            logger.info("Creating ai_throttle table")
            cache_db.action(
                """
                CREATE TABLE ai_throttle (
                    context TEXT,
                    scope TEXT,
                    scope_key TEXT,
                    last_attempt NUMERIC,
                    last_success NUMERIC,
                    PRIMARY KEY(context, scope, scope_key)
                )
                """
            )

        if not cache_db.has_table("ai_cache"):
            logger.info("Creating ai_cache table")
            cache_db.action(
                """
                CREATE TABLE ai_cache (
                    request_hash TEXT PRIMARY KEY,
                    response_json TEXT,
                    created NUMERIC,
                    expires NUMERIC,
                    context TEXT,
                    scope TEXT,
                    scope_key TEXT
                )
                """
            )
            cache_db.action("CREATE INDEX IF NOT EXISTS idx_ai_cache_expires ON ai_cache (expires)")

    def allow_search_for_show(self, show) -> bool:
        """
        Check if AI search is allowed for this show based on cooldown.

        Args:
            show: TVShow object

        Returns:
            True if AI search is allowed (cooldown expired or never attempted)
        """
        if not settings.AI_ENABLED or not settings.AI_SEARCH_ENABLED:
            return False

        if not self._check_budget():
            logger.debug("AI search blocked: budget exceeded")
            return False

        cooldown_days = settings.AI_SEARCH_COOLDOWN_DAYS_PER_SHOW
        cooldown_seconds = cooldown_days * 24 * 60 * 60

        scope_key = str(show.indexerid)
        last_attempt = self._get_last_attempt(self.CONTEXT_SEARCH, self.SCOPE_SHOW, scope_key)

        if last_attempt is None:
            return True

        elapsed = time.time() - last_attempt
        if elapsed < cooldown_seconds:
            remaining_hours = (cooldown_seconds - elapsed) / 3600
            logger.debug(
                f"AI search for show {show.name} blocked: cooldown ({remaining_hours:.1f}h remaining)"
            )
            return False

        return True

    def reserve_search_attempt(self, show) -> bool:
        """
        Atomically check cooldown and reserve a search slot for this show.

        This prevents race conditions where multiple threads might both pass
        the cooldown check before either records an attempt.

        Args:
            show: TVShow object

        Returns:
            True if reservation successful, False if blocked by cooldown/budget/concurrent request
        """
        scope_key = str(show.indexerid)

        with self._budget_lock:
            # Check if there's already a pending reservation for this show
            if scope_key in self._pending_reservations:
                # Check if reservation is stale (> 5 minutes old = likely crashed)
                if time.time() - self._pending_reservations[scope_key] < 300:
                    logger.debug(f"AI search for {show.name} blocked: concurrent request in progress")
                    return False
                # Stale reservation, clean it up
                del self._pending_reservations[scope_key]

            # Check standard cooldown (uses allow_search_for_show logic but inline)
            if not settings.AI_ENABLED or not settings.AI_SEARCH_ENABLED:
                return False

            if not self._check_budget():
                logger.debug("AI search blocked: budget exceeded")
                return False

            cooldown_days = settings.AI_SEARCH_COOLDOWN_DAYS_PER_SHOW
            cooldown_seconds = cooldown_days * 24 * 60 * 60
            last_attempt = self._get_last_attempt(self.CONTEXT_SEARCH, self.SCOPE_SHOW, scope_key)

            if last_attempt is not None:
                elapsed = time.time() - last_attempt
                if elapsed < cooldown_seconds:
                    remaining_hours = (cooldown_seconds - elapsed) / 3600
                    logger.debug(
                        f"AI search for show {show.name} blocked: cooldown ({remaining_hours:.1f}h remaining)"
                    )
                    return False

            # All checks passed - create reservation
            self._pending_reservations[scope_key] = time.time()
            logger.debug(f"AI search reservation created for {show.name}")
            return True

    def commit_search_attempt(self, show) -> None:
        """
        Commit a reserved search attempt after successful API call.

        This records the attempt in the database and releases the reservation.

        Args:
            show: TVShow object
        """
        scope_key = str(show.indexerid)

        # Record the attempt in database
        self.record_attempt(self.CONTEXT_SEARCH, self.SCOPE_SHOW, scope_key)

        # Release the reservation
        with self._budget_lock:
            self._pending_reservations.pop(scope_key, None)

        logger.debug(f"AI search attempt committed for {show.name}")

    def release_search_reservation(self, show) -> None:
        """
        Release a search reservation without recording an attempt.

        Use this when the API call fails or errors - we don't want to
        consume the cooldown for a failed attempt.

        Args:
            show: TVShow object
        """
        scope_key = str(show.indexerid)

        with self._budget_lock:
            self._pending_reservations.pop(scope_key, None)

        logger.debug(f"AI search reservation released for {show.name}")

    def allow_postprocess_for_file(self, file_path: str) -> bool:
        """
        Check if AI post-processing is allowed for this file based on cooldown.

        Args:
            file_path: Path to the file

        Returns:
            True if AI post-processing is allowed
        """
        if not settings.AI_ENABLED or not settings.AI_POSTPROCESS_MATCH_ENABLED:
            return False

        if not self._check_budget():
            logger.debug("AI post-process blocked: budget exceeded")
            return False

        cooldown_hours = settings.AI_POSTPROCESS_MATCH_COOLDOWN_HOURS_PER_FILE
        cooldown_seconds = cooldown_hours * 60 * 60

        scope_key = self.get_file_fingerprint(file_path)
        last_attempt = self._get_last_attempt(self.CONTEXT_POSTPROCESS, self.SCOPE_FILE, scope_key)

        if last_attempt is None:
            return True

        elapsed = time.time() - last_attempt
        if elapsed < cooldown_seconds:
            remaining_hours = (cooldown_seconds - elapsed) / 3600
            logger.debug(
                f"AI post-process for file blocked: cooldown ({remaining_hours:.1f}h remaining)"
            )
            return False

        return True

    def record_attempt(self, context: str, scope: str, scope_key: str) -> None:
        """
        Record that an AI request was attempted.

        Args:
            context: "search" or "postprocess"
            scope: "show" or "file"
            scope_key: Show ID or file fingerprint
        """
        now = time.time()
        cache_db = self._get_db()

        # Update or insert throttle record
        cache_db.action(
            """
            INSERT OR REPLACE INTO ai_throttle (context, scope, scope_key, last_attempt, last_success)
            VALUES (?, ?, ?, ?, (
                SELECT last_success FROM ai_throttle
                WHERE context = ? AND scope = ? AND scope_key = ?
            ))
            """,
            [context, scope, scope_key, now, context, scope, scope_key],
        )

        # Track in-memory budget counters (thread-safe)
        with self._budget_lock:
            self._hourly_calls.append(now)
            self._daily_calls.append(now)

        logger.debug(f"AI request recorded: {context}/{scope}/{scope_key[:20]}...")

    def record_success(self, context: str, scope: str, scope_key: str) -> None:
        """
        Record that an AI request succeeded.

        Args:
            context: "search" or "postprocess"
            scope: "show" or "file"
            scope_key: Show ID or file fingerprint
        """
        now = time.time()
        cache_db = self._get_db()

        cache_db.action(
            """
            UPDATE ai_throttle SET last_success = ?
            WHERE context = ? AND scope = ? AND scope_key = ?
            """,
            [now, context, scope, scope_key],
        )

    def record_search_attempt(self, show) -> None:
        """Convenience method to record a search attempt for a show."""
        self.record_attempt(self.CONTEXT_SEARCH, self.SCOPE_SHOW, str(show.indexerid))

    def record_search_success(self, show) -> None:
        """Convenience method to record a successful search for a show."""
        self.record_success(self.CONTEXT_SEARCH, self.SCOPE_SHOW, str(show.indexerid))

    def record_postprocess_attempt(self, file_path: str) -> None:
        """Convenience method to record a post-process attempt for a file."""
        fingerprint = self.get_file_fingerprint(file_path)
        self.record_attempt(self.CONTEXT_POSTPROCESS, self.SCOPE_FILE, fingerprint)

    def record_postprocess_success(self, file_path: str) -> None:
        """Convenience method to record a successful post-process for a file."""
        fingerprint = self.get_file_fingerprint(file_path)
        self.record_success(self.CONTEXT_POSTPROCESS, self.SCOPE_FILE, fingerprint)

    def _get_last_attempt(self, context: str, scope: str, scope_key: str) -> Optional[float]:
        """
        Get the timestamp of the last attempt for a given context/scope.

        Returns:
            Unix timestamp or None if never attempted
        """
        cache_db = self._get_db()
        result = cache_db.select(
            "SELECT last_attempt FROM ai_throttle WHERE context = ? AND scope = ? AND scope_key = ?",
            [context, scope, scope_key],
        )

        if result and result[0]["last_attempt"]:
            return float(result[0]["last_attempt"])
        return None

    def _check_budget(self) -> bool:
        """
        Check if we're within hourly and daily budget limits.

        Note: Budget counters are in-memory only and reset on restart.
        This is a soft limit for runaway prevention, not a hard cost cap.

        Returns:
            True if within budget
        """
        now = time.time()
        hour_ago = now - 3600
        day_ago = now - 86400

        with self._budget_lock:
            # Clean up old entries
            self._hourly_calls = [t for t in self._hourly_calls if t > hour_ago]
            self._daily_calls = [t for t in self._daily_calls if t > day_ago]

            hourly_limit = settings.AI_MAX_CALLS_PER_HOUR
            daily_limit = settings.AI_MAX_CALLS_PER_DAY

            if len(self._hourly_calls) >= hourly_limit:
                logger.warning(f"AI hourly budget exceeded ({len(self._hourly_calls)}/{hourly_limit})")
                return False

            if len(self._daily_calls) >= daily_limit:
                logger.warning(f"AI daily budget exceeded ({len(self._daily_calls)}/{daily_limit})")
                return False

        return True

    def get_budget_status(self) -> Dict[str, Any]:
        """
        Get current budget usage status.

        Returns:
            Dict with hourly and daily usage info
        """
        now = time.time()
        hour_ago = now - 3600
        day_ago = now - 86400

        with self._budget_lock:
            # Clean up old entries
            self._hourly_calls = [t for t in self._hourly_calls if t > hour_ago]
            self._daily_calls = [t for t in self._daily_calls if t > day_ago]

            return {
                "hourly_used": len(self._hourly_calls),
                "hourly_limit": settings.AI_MAX_CALLS_PER_HOUR,
                "daily_used": len(self._daily_calls),
                "daily_limit": settings.AI_MAX_CALLS_PER_DAY,
            }

    @staticmethod
    def get_file_fingerprint(file_path: str) -> str:
        """
        Generate a stable fingerprint for a file.

        Uses filename + file size + parent folder name to create a hash
        that survives moves within the same folder structure.

        Args:
            file_path: Path to the file

        Returns:
            SHA256 hash string (first 32 chars)
        """
        try:
            filename = os.path.basename(file_path)
            parent = os.path.basename(os.path.dirname(file_path))

            # Try to get file size, default to 0 if file doesn't exist
            try:
                file_size = os.path.getsize(file_path)
            except OSError:
                file_size = 0

            # Create fingerprint from components
            fingerprint_data = f"{filename}|{file_size}|{parent}"
            return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:32]

        except Exception as e:
            logger.warning(f"Error generating file fingerprint: {e}")
            # Fallback to just the filename
            return hashlib.sha256(file_path.encode()).hexdigest()[:32]

    def get_cached_response(
        self, request_hash: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get a cached AI response if it exists and hasn't expired.

        Args:
            request_hash: Hash of the request

        Returns:
            Cached response dict or None
        """
        import json

        cache_db = self._get_db()
        now = time.time()

        result = cache_db.select(
            "SELECT response_json FROM ai_cache WHERE request_hash = ? AND expires > ?",
            [request_hash, now],
        )

        if result:
            try:
                return json.loads(result[0]["response_json"])
            except (json.JSONDecodeError, KeyError):
                pass

        return None

    def cache_response(
        self,
        request_hash: str,
        response: Dict[str, Any],
        context: str,
        scope: str,
        scope_key: str,
    ) -> None:
        """
        Cache an AI response.

        Args:
            request_hash: Hash of the request
            response: Response dict to cache
            context: "search" or "postprocess"
            scope: "show" or "file"
            scope_key: Show ID or file fingerprint
        """
        import json

        cache_db = self._get_db()
        now = time.time()
        ttl_seconds = settings.AI_CACHE_TTL_DAYS * 24 * 60 * 60
        expires = now + ttl_seconds

        cache_db.action(
            """
            INSERT OR REPLACE INTO ai_cache
            (request_hash, response_json, created, expires, context, scope, scope_key)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [request_hash, json.dumps(response), now, expires, context, scope, scope_key],
        )

    def cleanup_expired_cache(self) -> int:
        """
        Remove expired cache entries.

        Returns:
            Number of entries removed
        """
        cache_db = self._get_db()
        now = time.time()

        # Get count before deletion
        result = cache_db.select("SELECT COUNT(*) as count FROM ai_cache WHERE expires < ?", [now])
        count = result[0]["count"] if result else 0

        if count > 0:
            cache_db.action("DELETE FROM ai_cache WHERE expires < ?", [now])
            logger.info(f"Cleaned up {count} expired AI cache entries")

        return count

    @staticmethod
    def generate_request_hash(prompt: str, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Generate a hash for a request to use as cache key.

        Args:
            prompt: The prompt text
            context: Optional context dict

        Returns:
            SHA256 hash string (first 32 chars)
        """
        import json

        data = prompt
        if context:
            data += json.dumps(context, sort_keys=True)

        return hashlib.sha256(data.encode()).hexdigest()[:32]
