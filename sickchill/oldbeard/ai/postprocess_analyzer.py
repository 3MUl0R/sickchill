"""
AI Post-Processing Analyzer for SickChill.

Provides AI-powered quality verification and issue detection
for downloaded files after they've been matched to a show/episode.

This is an optional step that can:
1. Verify the detected quality matches the actual file
2. Detect potential issues (wrong aspect ratio, small file size, etc.)
3. Recommend whether to proceed with processing
"""

from __future__ import annotations

import html
import logging
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from sickchill import settings
from sickchill.oldbeard import ui
from sickchill.oldbeard.ai import get_client, get_throttle, is_ai_available
from sickchill.oldbeard.common import Quality

if TYPE_CHECKING:
    from sickchill.tv import TVEpisode

logger = logging.getLogger(__name__)


def _escape_html(text: str) -> str:
    """Escape HTML entities in text for safe display in notifications."""
    if not text:
        return ""
    escaped = html.escape(str(text))
    if len(escaped) > 200:
        escaped = escaped[:197] + "..."
    return escaped


def _notify_analysis_issues(
    filename: str,
    issues: List[str],
) -> None:
    """
    Send a notification when AI detects issues with a file.

    Args:
        filename: Name of the file with issues
        issues: List of detected issues
    """
    if not settings.AI_NOTIFY_ON_FALLBACK_FAILURE:
        return

    try:
        safe_filename = html.escape(os.path.basename(filename)[:50])
        safe_issues = ", ".join(_escape_html(issue) for issue in issues[:3])
        ui.notifications.message(
            "AI File Analysis Issues",
            f"Issues detected in <i>{safe_filename}</i>: {safe_issues}"
        )
    except Exception as e:
        logger.debug(f"Failed to send AI analysis notification: {e}")


def _load_prompt_template() -> str:
    """Load the file analysis prompt template."""
    template_path = os.path.join(
        os.path.dirname(__file__), "prompts", "file_analysis.txt"
    )
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"File analysis prompt template not found: {template_path}")
        raise


def _get_media_info(file_path: str) -> Dict[str, Any]:
    """
    Get media information from the file.

    Args:
        file_path: Full path to the video file

    Returns:
        Dict with resolution, codecs, duration, etc.
    """
    info = {
        "width": 0,
        "height": 0,
        "video_codec": "unknown",
        "audio_codec": "unknown",
        "duration": 0,
        "container": os.path.splitext(file_path)[1].lstrip(".").upper() or "unknown",
    }

    try:
        from sickchill.helper.media_info import video_screen_size

        width, height = video_screen_size(file_path)
        if width and height:
            info["width"] = width
            info["height"] = height
    except Exception as e:
        logger.debug(f"Could not get video dimensions: {e}")

    # Try pymediainfo for more details if available
    try:
        from pymediainfo import MediaInfo

        media_info = MediaInfo.parse(file_path)
        for track in media_info.tracks:
            if track.track_type == "Video":
                if track.width:
                    info["width"] = track.width
                if track.height:
                    info["height"] = track.height
                if track.codec_id or track.format:
                    info["video_codec"] = track.codec_id or track.format
                if track.duration:
                    info["duration"] = int(float(track.duration) / 60000)  # Convert to minutes
            elif track.track_type == "Audio":
                if track.codec_id or track.format:
                    info["audio_codec"] = track.codec_id or track.format
    except ImportError:
        logger.debug("pymediainfo not available for detailed media analysis")
    except Exception as e:
        logger.debug(f"Could not parse media info: {e}")

    return info


def _validate_analysis_result(result: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Validate the AI's analysis result.

    Args:
        result: The AI response dict

    Returns:
        Tuple of (is_valid, rejection_reason)
    """
    # Check required fields
    if "quality_verified" not in result:
        return False, "Missing quality_verified field"

    if "proceed_with_processing" not in result:
        return False, "Missing proceed_with_processing field"

    if "confidence" not in result:
        return False, "Missing confidence field"

    confidence = result.get("confidence", 0)
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        return False, f"Invalid confidence value: {confidence}"

    issues = result.get("issues", [])
    if not isinstance(issues, list):
        return False, "Issues must be a list"

    return True, None


def analyze_file(
    file_path: str,
    episode: "TVEpisode",
    quality: int,
    release_group: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Use AI to analyze a file for quality verification and issue detection.

    This is an optional step called after the file has been matched.

    Args:
        file_path: Full path to the video file
        episode: The matched TVEpisode object
        quality: The detected quality value
        release_group: The release group if known

    Returns:
        Dict with quality_verified, issues, proceed_with_processing, etc.
        or None if AI is not available/configured
    """
    if not is_ai_available():
        logger.debug("AI post-process analysis not available")
        return None

    if not settings.AI_POSTPROCESS_ANALYZE_ENABLED:
        logger.debug("AI post-process analysis is disabled")
        return None

    # Check throttle - use atomic reservation to prevent race conditions
    # Analyzer uses the same throttle context as matcher since they share budget
    throttle = get_throttle()
    if not throttle.reserve_postprocess_attempt(file_path):
        logger.debug(f"AI post-process analysis blocked by cooldown or concurrent request")
        return None

    try:
        filename = os.path.basename(file_path)

        # Get file info
        try:
            file_size_mb = os.path.getsize(file_path) // (1024 * 1024)
        except Exception:
            file_size_mb = -1

        # Get media info
        media_info = _get_media_info(file_path)

        # Build the prompt
        template = _load_prompt_template()

        quality_string = Quality.qualityStrings.get(quality, "Unknown")

        prompt = template.format(
            filename=filename,
            file_size_mb=file_size_mb,
            container=media_info["container"],
            width=media_info["width"],
            height=media_info["height"],
            video_codec=media_info["video_codec"],
            audio_codec=media_info["audio_codec"],
            duration=media_info["duration"],
            show_name=episode.show.name if episode.show else "Unknown",
            season=episode.season,
            episode=episode.episode,
            quality=quality_string,
            release_group=release_group or "Unknown",
        )

        # Call AI
        client = get_client()
        if not client:
            logger.warning("AI client not available")
            throttle.release_postprocess_reservation(file_path)
            return None

        logger.info(f"AI analyzing file quality: {filename}")

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
        is_valid, rejection_reason = _validate_analysis_result(response)
        if not is_valid:
            logger.warning(f"AI returned invalid analysis result: {rejection_reason}")
            return None

        # Extract results
        quality_verified = response.get("quality_verified", False)
        issues = response.get("issues", [])
        proceed = response.get("proceed_with_processing", True)
        confidence = response.get("confidence", 0.0)
        quality_assessment = response.get("quality_assessment", "")
        notes = response.get("notes", "")

        logger.debug(
            f"AI analysis: quality_verified={quality_verified}, "
            f"issues={len(issues)}, proceed={proceed}, confidence={confidence}"
        )

        # Log and notify about issues
        if issues:
            logger.info(f"AI detected issues in {filename}: {issues}")
            if not proceed:
                _notify_analysis_issues(filename, issues)

        # Record success
        throttle.record_postprocess_success(file_path)

        result = {
            "quality_verified": quality_verified,
            "quality_assessment": quality_assessment,
            "issues": issues,
            "proceed_with_processing": proceed,
            "confidence": confidence,
            "notes": notes,
        }

        return result

    except Exception as e:
        logger.error(f"AI file analysis failed: {e}")
        # Release reservation on error so cooldown isn't consumed
        throttle.release_postprocess_reservation(file_path)
        return None


def should_block_processing(analysis_result: Optional[Dict[str, Any]]) -> bool:
    """
    Determine if processing should be blocked based on AI analysis.

    Args:
        analysis_result: The result from analyze_file()

    Returns:
        True if processing should be blocked, False otherwise
    """
    if not analysis_result:
        # No analysis result - don't block
        return False

    if not settings.AI_POSTPROCESS_DETECT_ISSUES:
        # Issue detection is disabled - don't block
        return False

    # Check if AI recommends not processing
    if not analysis_result.get("proceed_with_processing", True):
        confidence = analysis_result.get("confidence", 0.0)
        # Only block if AI is confident about its assessment
        if confidence >= settings.AI_CONFIDENCE_THRESHOLD:
            return True

    return False
