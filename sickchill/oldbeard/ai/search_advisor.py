"""
AI Search Advisor for SickChill.

Provides AI-powered analysis of search results when the rule-based
picker cannot make a confident selection.

This module is called as a fallback when:
1. pick_best_result() returns None (no result passed filters)
2. A show has repeated failed downloads (failed download retry loop)
"""

from __future__ import annotations

import html
import json
import logging
import os
from typing import TYPE_CHECKING, List, Optional, Tuple

from sickchill import settings
from sickchill.oldbeard import db, show_name_helpers, ui
from sickchill.oldbeard.ai import get_client, get_feedback_manager, get_preferences_manager, get_throttle, is_ai_available
from sickchill.oldbeard.common import Quality

if TYPE_CHECKING:
    from sickchill.providers.result_classes import SearchResult
    from sickchill.tv import TVEpisode, TVShow

logger = logging.getLogger(__name__)


def _escape_html(text: str) -> str:
    """
    Escape HTML entities in text for safe display in notifications.

    Args:
        text: Raw text that may contain HTML

    Returns:
        HTML-escaped text safe for display
    """
    if not text:
        return ""
    # Escape HTML entities and strip any remaining tags
    escaped = html.escape(str(text))
    # Truncate very long reasons
    if len(escaped) > 200:
        escaped = escaped[:197] + "..."
    return escaped


def _notify_ai_fallback_failure(
    show_name: str,
    episode_info: str,
    reason: str,
) -> None:
    """
    Send a notification when AI fallback fails to find a result.

    Args:
        show_name: Name of the show
        episode_info: Episode identifier (e.g., "S01E05")
        reason: Brief explanation of why AI couldn't select a result
    """
    if not settings.AI_NOTIFY_ON_FALLBACK_FAILURE:
        return

    try:
        # Escape HTML in reason since it may contain untrusted AI content
        safe_reason = _escape_html(reason)
        ui.notifications.message(
            "AI Search Fallback Failed",
            f"Couldn't find a suitable download for <i>{html.escape(show_name)}</i> {html.escape(episode_info)}. Reason: {safe_reason}",
        )
    except Exception as e:
        logger.debug(f"Failed to send AI fallback notification: {e}")


def _load_prompt_template() -> str:
    """Load the search selection prompt template."""
    template_path = os.path.join(os.path.dirname(__file__), "prompts", "search_selection.txt")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"Search selection prompt template not found: {template_path}")
        raise


def _format_results_for_prompt(
    results: List["SearchResult"],
) -> str:
    """
    Format search results as JSON for the AI prompt.

    Args:
        results: List of SearchResult objects

    Returns:
        JSON string of results with relevant attributes
    """
    formatted = []
    for i, result in enumerate(results):
        result_data = {
            "index": i,
            "name": result.name,
            "quality": Quality.qualityStrings.get(result.quality, "Unknown"),
            "size_mb": result.size // (1024 * 1024) if result.size > 0 else -1,
            "provider": result.provider.name if result.provider else "Unknown",
            "release_group": result.release_group or "Unknown",
            "is_torrent": getattr(result, "is_torrent", False),
        }
        # Add seeders/leechers if available (not all providers expose these)
        if hasattr(result, "seeders"):
            result_data["seeders"] = result.seeders
        if hasattr(result, "leechers"):
            result_data["leechers"] = result.leechers
        formatted.append(result_data)
    return json.dumps(formatted, indent=2)


def _get_failed_releases_for_show(show: "TVShow") -> List[str]:
    """
    Get list of previously failed release names for a show.

    Args:
        show: TVShow object

    Returns:
        List of failed release names (truncated for prompt brevity)
    """
    try:
        # Query failed.db for releases matching this show
        # The failed table stores release names, which typically contain show name
        failed_db = db.DBConnection("failed.db")

        # Get recent failed releases (last 50 to keep prompt manageable)
        # Note: failed.db stores normalized release names
        results = failed_db.select('SELECT "release" FROM failed ORDER BY rowid DESC LIMIT 50')

        failed_names = []
        show_name_lower = show.name.lower().replace(" ", "").replace(".", "")

        for row in results:
            release = row["release"]
            # Basic filtering: check if release might be for this show
            release_lower = release.lower().replace("_", "")
            if show_name_lower in release_lower:
                failed_names.append(release)
                if len(failed_names) >= 10:  # Limit to 10 most recent
                    break

        return failed_names
    except Exception as e:
        logger.debug(f"Could not get failed releases: {e}")
        return []


def _get_preferred_words(show: "TVShow") -> str:
    """
    Get preferred words for a show (show-level + global).

    Args:
        show: TVShow object

    Returns:
        Comma-separated preferred words string
    """
    words = []

    # Show-level preferred words (rls_prefer_words, not rls_require_words)
    if hasattr(show, "rls_prefer_words") and show.rls_prefer_words:
        words.extend([w.strip() for w in show.rls_prefer_words.split(",") if w.strip()])

    # Global preferred words
    if settings.PREFER_WORDS:
        words.extend([w.strip() for w in settings.PREFER_WORDS.split(",") if w.strip()])

    return ", ".join(words) if words else ""


def _validate_ai_selection(
    result: "SearchResult",
    show: "TVShow",
    episode: "TVEpisode",
    relax_filters: bool = False,
) -> Tuple[bool, Optional[str]]:
    """
    Re-validate AI's selection against hard filters.

    Since AI is called when pick_best_result() rejected everything,
    we can optionally relax certain filters while maintaining critical safety.

    Args:
        result: The AI-selected SearchResult
        show: TVShow object
        episode: TVEpisode object
        relax_filters: If True, skip soft filters (require words, prefer words)
                       but keep hard filters (quality, failed history, anime groups)

    Returns:
        Tuple of (is_valid: bool, rejection_reason: str or None)
    """
    # Import History here to allow proper patching in tests
    from sickchill.show.History import History

    # 1. HARD FILTER: Check quality allowlist (never relax this)
    allowed_qualities, preferred_qualities = Quality.splitQuality(show.quality)
    all_qualities = allowed_qualities + preferred_qualities
    if result.quality not in all_qualities:
        return False, f"Quality {Quality.qualityStrings.get(result.quality, 'Unknown')} not in allowed list"

    # 2. HARD FILTER: Check anime release groups (never relax this)
    if show.is_anime and hasattr(show, "release_groups"):
        if not show.release_groups.is_valid(result):
            return False, "Release group not in anime whitelist/blacklist"

    # 3. SOFT FILTER: Check require/ignore words via filter_bad_releases
    # This can be relaxed if AI_SEARCH_ALLOW_RELAX_FILTERS is enabled
    if not relax_filters:
        if not show_name_helpers.filter_bad_releases(result.name, parse=False, show=show):
            return False, "Release contains ignored words or missing required words"
    else:
        # Even with relaxed filters, still check for truly bad releases
        # (scene exceptions, obvious fakes) but skip require/ignore word checks
        # Check only the global bad release patterns, not show-specific words
        if not show_name_helpers.filter_bad_releases(result.name, parse=False, show=None):
            return False, "Release matches global bad release patterns"

    # 4. HARD FILTER: Check failed download history (never relax this)
    if hasattr(result, "size") and result.size > 0:
        if settings.USE_FAILED_DOWNLOADS:
            provider_name = result.provider.name if result.provider else ""
            if History().has_failed(result.name, result.size, provider_name):
                return False, "Release previously failed download"

    # 5. HARD FILTER: Verify result is for correct show (sanity check)
    if result.show and result.show is not show:
        return False, "Result is for a different show"

    return True, None


def analyze_search_results(
    results: List["SearchResult"],
    episode: "TVEpisode",
    is_failed_retry: bool = False,
) -> Optional["SearchResult"]:
    """
    Use AI to analyze and select the best search result.

    This is called as a fallback when:
    1. pick_best_result() returns None
    2. A show is in a failed download retry loop

    Args:
        results: List of SearchResult objects to analyze
        episode: The episode we're searching for
        is_failed_retry: True if this is a retry after failed download

    Returns:
        The selected SearchResult, or None if AI cannot make a selection
        or AI is not available/configured
    """
    if not results:
        logger.debug("No results to analyze")
        return None

    if not is_ai_available():
        logger.debug("AI search fallback not available (not configured or disabled)")
        return None

    show = episode.show
    if not show:
        logger.debug("Episode has no associated show")
        return None

    # Check throttle - respect per-show cooldown
    # Use atomic check-and-reserve to prevent race conditions
    throttle = get_throttle()
    if not throttle.reserve_search_attempt(show):
        logger.debug(f"AI search for {show.name} blocked by cooldown or concurrent request")
        return None

    # Format episode info for notifications (needed in exception handler)
    episode_info = f"S{episode.season:02d}E{episode.episode:02d}"

    try:
        # Build the prompt
        template = _load_prompt_template()

        # Get quality info
        allowed_qualities, preferred_qualities = Quality.splitQuality(show.quality)
        quality_names = [Quality.qualityStrings.get(q, "Unknown") for q in (preferred_qualities or allowed_qualities)]
        target_quality = ", ".join(quality_names) if quality_names else "Any"

        # Get preferred/ignored words
        preferred_words = _get_preferred_words(show)
        ignored_words = show.rls_ignore_words or ""

        # Get failed releases for context
        failed_releases = _get_failed_releases_for_show(show)
        failed_str = ", ".join(failed_releases[:5]) if failed_releases else "None"

        # Format results
        results_json = _format_results_for_prompt(results)

        # Build prompt
        prompt = template.format(
            show_name=show.name,
            season=episode.season,
            episode=episode.episode,
            quality=target_quality,
            preferred_words=preferred_words or "None",
            ignored_words=ignored_words or "None",
            results_json=results_json,
            failed_releases=failed_str,
        )

        # Add context note for failed retry
        system_prompt = None
        if is_failed_retry:
            system_prompt = (
                "IMPORTANT: This show has had multiple failed downloads. "
                "Be extra cautious and prioritize releases with good seeder "
                "counts and reputable release groups. Avoid anything that "
                "looks like it might be incomplete or fake."
            )

        # Call AI
        client = get_client()
        if not client:
            logger.warning("AI client not available")
            throttle.release_search_reservation(show)
            return None

        logger.info(f"AI analyzing {len(results)} search results for {show.name} {episode_info}")

        response = client.analyze(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=512,
            cost_context="search",
            cost_scope_key=str(show.indexerid),
        )

        # Commit the attempt now that API call succeeded
        throttle.commit_search_attempt(show)

        # Parse response
        selected_index = response.get("selected_index", -1)
        confidence = response.get("confidence", 0.0)
        reasoning = response.get("reasoning", "No reasoning provided")

        logger.debug(f"AI response: index={selected_index}, confidence={confidence}, reason={reasoning}")

        # Validate response
        if selected_index < 0:
            logger.info(f"AI found no acceptable result: {reasoning}")
            _notify_ai_fallback_failure(show.name, episode_info, reasoning)
            return None

        if selected_index >= len(results):
            logger.warning(f"AI returned invalid index {selected_index} (only {len(results)} results)")
            _notify_ai_fallback_failure(show.name, episode_info, "AI returned an invalid selection")
            return None

        # Check confidence threshold (per-show or global)
        prefs_manager = get_preferences_manager()
        confidence_threshold = prefs_manager.get_confidence_threshold(show)
        if confidence < confidence_threshold:
            reason = f"AI confidence too low ({confidence:.0%})"
            logger.info(f"AI confidence {confidence:.2f} below threshold {confidence_threshold:.2f}: {reasoning}")
            _notify_ai_fallback_failure(show.name, episode_info, reason)
            return None

        # Get selected result
        selected_result = results[selected_index]

        # Re-validate against hard filters
        # Allow relaxed filters if AI_SEARCH_ALLOW_RELAX_FILTERS is enabled
        # This is the key to making AI useful: it can select results that failed
        # soft filters (require/prefer words) but still pass hard filters (quality, etc.)
        relax_filters = settings.AI_SEARCH_ALLOW_RELAX_FILTERS
        is_valid, rejection_reason = _validate_ai_selection(selected_result, show, episode, relax_filters=relax_filters)

        if not is_valid:
            logger.warning(f"AI selected result #{selected_index} ({selected_result.name}) failed re-validation: {rejection_reason}")
            _notify_ai_fallback_failure(show.name, episode_info, f"AI selection blocked by filters: {rejection_reason}")
            return None

        # Success - return selected result
        logger.info(f"AI selected result #{selected_index}: {selected_result.name} (confidence: {confidence:.2f}, reason: {reasoning})")

        # Record success
        throttle.record_search_success(show)

        # Record decision for feedback tracking
        try:
            from sickchill.oldbeard.ai.feedback import DecisionType

            feedback_mgr = get_feedback_manager()
            feedback_mgr.record_decision(
                decision_type=DecisionType.SEARCH_SELECTION,
                input_summary=f"{len(results)} results for {show.name} {episode_info}",
                output_summary=f"Selected #{selected_index}: {selected_result.name[:100]}",
                confidence=confidence,
                reasoning=reasoning,
                raw_response=response,
                show_id=show.indexerid,
            )
        except Exception as e:
            logger.debug(f"Failed to record AI decision: {e}")

        return selected_result

    except Exception as e:
        logger.error(f"AI search analysis failed: {e}")
        # Release reservation on error so cooldown isn't consumed
        throttle.release_search_reservation(show)
        # Try to send notification if we have episode info
        try:
            _notify_ai_fallback_failure(show.name, episode_info, f"Analysis error: {e}")
        except Exception:
            pass  # Don't fail on notification error
        return None


def should_use_ai_fallback(
    results: List["SearchResult"],
    show: "TVShow",
    picked_result: Optional["SearchResult"],
) -> bool:
    """
    Determine if AI fallback should be used for search.

    Args:
        results: Original list of search results
        show: The show being searched
        picked_result: Result from pick_best_result() (may be None)

    Returns:
        True if AI fallback should be attempted
    """
    # Must have results to analyze
    if not results:
        return False

    # Check minimum results threshold
    if len(results) < settings.AI_SEARCH_MIN_RESULTS:
        logger.debug(f"Not enough results for AI analysis ({len(results)} < {settings.AI_SEARCH_MIN_RESULTS})")
        return False

    # Check per-show preferences (handles global settings too)
    prefs_manager = get_preferences_manager()
    has_result = picked_result is not None
    return prefs_manager.should_use_ai_search(show, has_result)
