"""
AI Post-Processing Matcher for SickChill.

Provides AI-powered file identification when the rule-based
parser cannot determine the show/episode from the filename.

This module is called as a fallback when:
1. _find_info() cannot identify the show/season/episode
2. History lookup and name parsing all failed
"""

from __future__ import annotations

import html
import json
import logging
import os
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from sickchill import settings
from sickchill.oldbeard import ui
from sickchill.oldbeard.ai import get_client, get_feedback_manager, get_preferences_manager, get_throttle, is_ai_available

if TYPE_CHECKING:
    from sickchill.tv import TVShow

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
    escaped = html.escape(str(text))
    if len(escaped) > 200:
        escaped = escaped[:197] + "..."
    return escaped


def _notify_match_failure(
    filename: str,
    reason: str,
) -> None:
    """
    Send a notification when AI matching fails.

    Args:
        filename: Name of the file that couldn't be matched
        reason: Brief explanation of why AI couldn't match
    """
    if not settings.AI_NOTIFY_ON_FALLBACK_FAILURE:
        return

    try:
        safe_reason = _escape_html(reason)
        safe_filename = html.escape(os.path.basename(filename)[:50])
        ui.notifications.message(
            "AI Post-Process Match Failed",
            f"Couldn't identify file <i>{safe_filename}</i>. "
            f"Reason: {safe_reason}"
        )
    except Exception as e:
        logger.debug(f"Failed to send AI match notification: {e}")


def _load_prompt_template() -> str:
    """Load the file matching prompt template."""
    template_path = os.path.join(
        os.path.dirname(__file__), "prompts", "file_match.txt"
    )
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"File match prompt template not found: {template_path}")
        raise


def _get_candidate_shows(filename: str, folder_name: str, release_name: Optional[str] = None, limit: int = 25) -> List[Dict[str, Any]]:
    """
    Get candidate shows from the user's library based on fuzzy name matching.

    Args:
        filename: The filename to match against
        folder_name: The folder name to match against
        release_name: Optional release name for additional matching context
        limit: Maximum number of candidates to return

    Returns:
        List of candidate show dicts with indexer_id, name, and aliases
    """
    candidates = []
    # Build search text including release_name if available
    search_parts = [folder_name, filename]
    if release_name:
        search_parts.append(release_name)
    search_text = " ".join(search_parts).lower()

    # Get all shows from the user's library via settings.show_list
    all_shows = settings.show_list or []

    # First pass: score by show name only (cheap operation)
    preliminary_scores = []
    for show in all_shows:
        if not show:
            continue

        # Calculate similarity score against show name
        show_name_lower = show.name.lower()
        score = SequenceMatcher(None, search_text, show_name_lower).ratio()

        # Also check if show name appears as substring (boosts common matches)
        if show_name_lower in search_text:
            score = max(score, 0.7)

        preliminary_scores.append((show, score))

    # Sort by preliminary score and only check aliases for top candidates
    # This optimizes the expensive scene_exceptions lookup
    preliminary_scores.sort(key=lambda x: x[1], reverse=True)
    top_candidates = preliminary_scores[:limit * 2]  # Check more to account for alias boosts

    for show, base_score in top_candidates:
        score = base_score
        alias_names = []

        # Only check scene exceptions for promising candidates
        if score >= 0.3:
            try:
                from sickchill.oldbeard import scene_exceptions
                exceptions = scene_exceptions.get_all_scene_exceptions(show.indexerid)
                if exceptions:
                    # exceptions is {season: [{"show_name": "...", "custom": True}, ...]}
                    for season_exceptions in exceptions.values():
                        for exc_dict in season_exceptions:
                            if isinstance(exc_dict, dict) and "show_name" in exc_dict:
                                alias_name = exc_dict["show_name"]
                                alias_names.append(alias_name)
                                # Check similarity against this alias
                                alias_score = SequenceMatcher(None, search_text, alias_name.lower()).ratio()
                                if alias_score > score:
                                    score = alias_score
            except Exception:
                pass

        candidates.append({
            "indexer_id": show.indexerid,
            "indexer": show.indexer,
            "name": show.name,
            "aliases": alias_names[:5],  # Limit aliases for prompt
            "score": score,
        })

    # Sort by final score descending and return top candidates
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:limit]


def _format_candidates_for_prompt(candidates: List[Dict[str, Any]]) -> str:
    """
    Format candidate shows as JSON for the AI prompt.

    Args:
        candidates: List of candidate show dicts

    Returns:
        JSON string of candidates
    """
    formatted = []
    for candidate in candidates:
        formatted.append({
            "indexer_id": candidate["indexer_id"],
            "indexer": candidate["indexer"],
            "name": candidate["name"],
            "aliases": candidate.get("aliases", []),
        })
    return json.dumps(formatted, indent=2)


def _get_file_info(file_path: str) -> Dict[str, Any]:
    """
    Get basic file information for the prompt.

    Args:
        file_path: Full path to the video file

    Returns:
        Dict with file size, duration (if available)
    """
    info = {
        "file_size_mb": -1,
        "duration": "unknown",
    }

    try:
        if os.path.exists(file_path):
            info["file_size_mb"] = os.path.getsize(file_path) // (1024 * 1024)
    except Exception:
        pass

    # Try to get duration using pymediainfo if available
    try:
        from sickchill.helper.media_info import video_screen_size
        # We can't easily get duration from the current helper, so skip for now
        # This could be enhanced later
    except Exception:
        pass

    return info


def _validate_match_result(
    result: Dict[str, Any],
    candidates: List[Dict[str, Any]],
) -> tuple[bool, Optional[str]]:
    """
    Validate the AI's match result.

    Args:
        result: The AI response dict
        candidates: The candidate shows that were provided to AI

    Returns:
        Tuple of (is_valid, rejection_reason)
    """
    show_id = result.get("show_indexer_id", -1)

    # -1 means AI couldn't match - this is valid but indicates no match
    if show_id == -1:
        return True, None

    # Check that the show ID is in our candidate list
    valid_ids = {c["indexer_id"] for c in candidates}
    if show_id not in valid_ids:
        return False, f"AI returned show ID {show_id} which was not in candidate list"

    # Basic sanity checks
    season = result.get("season")
    if season is not None and (not isinstance(season, int) or season < 0):
        return False, f"Invalid season number: {season}"

    episodes = result.get("episodes", [])
    if episodes:
        if not isinstance(episodes, list):
            return False, "Episodes must be a list"
        for ep in episodes:
            if not isinstance(ep, int) or ep < 0:
                return False, f"Invalid episode number: {ep}"

    # For a successful match, require season and episodes to be present
    # Otherwise the post-processor will fail anyway
    if season is None:
        return False, "Match found but season is missing"
    if not episodes:
        return False, "Match found but episodes list is empty"

    return True, None


def match_file(
    file_path: str,
    filename: str,
    folder_name: str,
    release_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Use AI to identify show/season/episode when normal parsing fails.

    This is called as a fallback when:
    1. History lookup found nothing
    2. Name parsing couldn't identify the content

    Args:
        file_path: Full path to the video file
        filename: The filename only
        folder_name: The containing folder name
        release_name: Optional release name if known

    Returns:
        Dict with show_indexer_id, season, episodes, confidence, reasoning
        or None if AI is not available/configured or can't make a match
    """
    if not is_ai_available():
        logger.debug("AI post-process matching not available (not configured or disabled)")
        return None

    if not settings.AI_POSTPROCESS_MATCH_ENABLED:
        logger.debug("AI post-process matching is disabled")
        return None

    # Check throttle - use atomic reservation to prevent race conditions
    throttle = get_throttle()
    if not throttle.reserve_postprocess_attempt(file_path):
        logger.debug(f"AI post-process for {filename} blocked by cooldown or concurrent request")
        return None

    try:
        # Get candidate shows from library (pass release_name for better matching)
        candidates = _get_candidate_shows(filename, folder_name, release_name=release_name)
        if not candidates:
            logger.debug("No candidate shows in library for AI matching")
            throttle.release_postprocess_reservation(file_path)
            return None

        # Get file info
        file_info = _get_file_info(file_path)

        # Build the prompt
        template = _load_prompt_template()
        candidates_json = _format_candidates_for_prompt(candidates)

        prompt = template.format(
            filename=filename,
            folder_name=folder_name,
            relative_path=os.path.join(folder_name, filename),
            file_size_mb=file_info["file_size_mb"],
            duration=file_info["duration"],
            release_name=release_name or "Not available",
            candidate_shows_json=candidates_json,
        )

        # Call AI
        client = get_client()
        if not client:
            logger.warning("AI client not available")
            throttle.release_postprocess_reservation(file_path)
            return None

        logger.info(f"AI analyzing file for matching: {filename}")

        # Get file fingerprint for cost tracking
        file_fingerprint = throttle.get_file_fingerprint(file_path)

        response = client.analyze(
            prompt=prompt,
            max_tokens=512,
            cost_context="postprocess",
            cost_scope_key=file_fingerprint,
        )

        # Commit the attempt now that API call succeeded
        throttle.commit_postprocess_attempt(file_path)

        # Validate response
        is_valid, rejection_reason = _validate_match_result(response, candidates)
        if not is_valid:
            logger.warning(f"AI returned invalid match result: {rejection_reason}")
            _notify_match_failure(filename, f"Invalid response: {rejection_reason}")
            return None

        # Check if AI found a match
        show_id = response.get("show_indexer_id", -1)
        confidence = response.get("confidence", 0.0)
        reasoning = response.get("reasoning", "No reasoning provided")

        logger.debug(f"AI match response: show_id={show_id}, confidence={confidence}, reason={reasoning}")

        if show_id == -1:
            logger.info(f"AI could not identify file: {reasoning}")
            _notify_match_failure(filename, reasoning)
            return None

        # Check confidence threshold
        min_confidence = settings.AI_POSTPROCESS_MATCH_MIN_CONFIDENCE
        if confidence < min_confidence:
            reason = f"AI confidence too low ({confidence:.0%})"
            logger.info(
                f"AI match confidence {confidence:.2f} below threshold {min_confidence:.2f}: {reasoning}"
            )
            _notify_match_failure(filename, reason)
            return None

        # Find the indexer for the matched show
        matched_candidate = next(
            (c for c in candidates if c["indexer_id"] == show_id),
            None
        )

        # Success - return match result
        result = {
            "show_indexer_id": show_id,
            "indexer": matched_candidate["indexer"] if matched_candidate else 1,
            "season": response.get("season"),
            "episodes": response.get("episodes", []),
            "confidence": confidence,
            "reasoning": reasoning,
        }

        logger.info(
            f"AI matched file to show {show_id}: "
            f"S{result['season']}E{result['episodes']} "
            f"(confidence: {confidence:.2f})"
        )

        # Record success
        throttle.record_postprocess_success(file_path)

        # Record decision for feedback tracking
        try:
            from sickchill.oldbeard.ai.feedback import DecisionType

            feedback_mgr = get_feedback_manager()
            show_name = matched_candidate["name"] if matched_candidate else f"ID:{show_id}"
            feedback_mgr.record_decision(
                decision_type=DecisionType.FILE_MATCH,
                input_summary=f"Match file: {filename[:80]}",
                output_summary=f"Matched to {show_name} S{result['season']}E{result['episodes']}",
                confidence=confidence,
                reasoning=reasoning,
                raw_response=response,
                show_id=show_id,
            )
        except Exception as e:
            logger.debug(f"Failed to record AI decision: {e}")

        return result

    except Exception as e:
        logger.error(f"AI file matching failed: {e}")
        # Release reservation on error so cooldown isn't consumed
        throttle.release_postprocess_reservation(file_path)
        _notify_match_failure(filename, f"Analysis error: {e}")
        return None


def should_use_ai_match(
    show: Optional["TVShow"],
    season: Optional[int],
    episodes: List[int],
) -> bool:
    """
    Determine if AI matching should be attempted.

    Args:
        show: The show object (None if not identified)
        season: The season number (None if not identified)
        episodes: List of episode numbers (empty if not identified)

    Returns:
        True if AI matching should be attempted
    """
    # AI must be enabled at global level
    if not settings.AI_ENABLED or not settings.AI_POSTPROCESS_MATCH_ENABLED:
        return False

    # Determine if we have a complete match
    has_match = show is not None and season is not None and episodes

    # If we have the show, check per-show preferences
    if show is not None:
        prefs_manager = get_preferences_manager()
        return prefs_manager.should_use_ai_postprocess(show, has_match)

    # If we couldn't identify the show at all, try AI (can't check per-show prefs)
    if show is None:
        return True

    return False
