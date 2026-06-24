"""
AI Search Result -> Episode Matcher for SickChill.

When searching for an anime, providers often return releases that the rule-based parser
cannot map to a wanted episode: unknown fansub aliases, season-split titles whose metadata
the indexer records incorrectly, or alternate romaji names. Those releases are dropped by
``GenericProvider.find_search_results`` (either at the name-parse exception or at the
"doesn't seem to match an episode" skip).

This module is the AI gap-filler for that residue: given the wanted episodes and the dropped
release titles for a single anime show, it asks the AI to map each release to a wanted
(season, episode) using its broader knowledge of anime titles and absolute numbering.

It only resolves *identity*. The provider still runs ``want_episode`` on every mapping and
the downstream ``pick_best_result``/quality/failed-history checks still apply — AI never
bypasses quality or "is this actually wanted" gating.
"""

from __future__ import annotations

import json
import logging
import math
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from sickchill import settings
from sickchill.oldbeard.ai import get_client, get_feedback_manager, get_preferences_manager, get_throttle, is_ai_available
from sickchill.oldbeard.ai.throttle import ThrottleManager

if TYPE_CHECKING:
    from sickchill.tv import TVEpisode, TVShow

logger = logging.getLogger(__name__)

# Cap on how many dropped releases we hand to the AI in one request (keeps the prompt and
# response bounded; the throttle already bounds how often we call at all).
MAX_RELEASES_PER_REQUEST = 40


def _is_int(value: Any) -> bool:
    """True only for a real int. Excludes bool (a Python int subclass)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_valid_confidence(value: Any) -> bool:
    """True only for an explicit, finite numeric confidence in [0.0, 1.0] (not bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0.0 <= value <= 1.0


def _load_prompt_template() -> str:
    """Load the search-match prompt template."""
    template_path = os.path.join(os.path.dirname(__file__), "prompts", "search_match.txt")
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def _get_aliases(show: "TVShow") -> List[str]:
    """Collect scene-exception aliases for the show (best effort)."""
    aliases: List[str] = []
    try:
        from sickchill.oldbeard import scene_exceptions

        exceptions = scene_exceptions.get_all_scene_exceptions(show.indexerid) or {}
        for season_exceptions in exceptions.values():
            for exc in season_exceptions:
                if isinstance(exc, dict) and exc.get("show_name"):
                    aliases.append(exc["show_name"])
    except Exception as error:
        logger.debug(f"Could not load scene exceptions for AI search match: {error}")
    # De-dupe while preserving order, bound the count for the prompt.
    seen = set()
    unique = []
    for alias in aliases:
        key = alias.lower()
        if key not in seen:
            seen.add(key)
            unique.append(alias)
    return unique[:15]


def _build_wanted_json(episodes: List["TVEpisode"]) -> str:
    """Serialize the wanted episodes for the prompt."""
    wanted = []
    for episode in episodes:
        wanted.append(
            {
                "season": episode.season,
                "episode": episode.episode,
                "absolute": getattr(episode, "absolute_number", None) or None,
                "title": (episode.name or "")[:120],
            }
        )
    return json.dumps(wanted, indent=2)


def _build_releases_json(items: List[Dict[str, Any]]) -> str:
    """Serialize the dropped releases (index + title + size) for the prompt."""
    releases = []
    for index, item in enumerate(items):
        size = item.get("size", -1) or -1
        size_mb = size // (1024 * 1024) if size and size > 0 else -1
        releases.append({"index": index, "title": (item.get("title") or "")[:200], "size_mb": size_mb})
    return json.dumps(releases, indent=2)


def match_results(
    show: "TVShow",
    episodes: List["TVEpisode"],
    unmatched_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Ask the AI to map dropped anime releases to wanted episodes of ``show``.

    Args:
        show: The show currently being searched.
        episodes: The wanted episodes being searched for (the only valid targets).
        unmatched_items: Dropped releases. Each is a dict with at least ``title`` and the raw
            provider ``item``; ``url``/``size``/``seeders``/``leechers`` are passed through.

    Returns:
        A list of confident mappings, each a copy of the input record augmented with
        ``season``/``episode``/``confidence``/``reasoning``. Empty list if AI is unavailable,
        disabled, throttled, or makes no confident mapping. The caller is responsible for
        running ``want_episode`` and building/selecting the actual result.
    """
    if not unmatched_items or not episodes:
        return []

    if not is_ai_available():
        logger.debug("AI search matching not available (not configured or disabled)")
        return []

    if not settings.AI_SEARCH_ENABLED:
        logger.debug("AI search matching is disabled (AI_SEARCH_ENABLED off)")
        return []

    prefs_manager = get_preferences_manager()
    # Respect per-show AI-search preferences; these releases are an unmatched fallback case.
    if not prefs_manager.should_use_ai_search(show, has_result=False):
        logger.debug(f"AI search matching skipped for {show.name} (per-show preference)")
        return []

    # Independent per-show cooldown from the selection advisor; shares the global budget.
    throttle = get_throttle()
    if not throttle.reserve_search_attempt(show, context=ThrottleManager.CONTEXT_SEARCH_MATCH):
        logger.debug(f"AI search matching for {show.name} blocked by cooldown or concurrent request")
        return []

    items = unmatched_items[:MAX_RELEASES_PER_REQUEST]
    if len(unmatched_items) > len(items):
        logger.debug(f"AI search matching: capping {len(unmatched_items)} dropped releases to {len(items)} for {show.name}")

    try:
        template = _load_prompt_template()
        aliases = _get_aliases(show)
        prompt = template.format(
            show_name=show.name,
            aliases=", ".join(aliases) if aliases else "None",
            wanted_json=_build_wanted_json(episodes),
            releases_json=_build_releases_json(items),
        )

        client = get_client()
        if not client:
            logger.warning("AI client not available")
            throttle.release_search_reservation(show, context=ThrottleManager.CONTEXT_SEARCH_MATCH)
            return []

        logger.info(f"AI matching {len(items)} unmatched anime releases for {show.name}")

        response, was_cached = client.analyze(
            prompt=prompt,
            max_tokens=1024,
            cost_context="search_match",
            cost_scope_key=str(show.indexerid),
            return_meta=True,
        )

        # A cache hit still records the cooldown but must not bill the call budget.
        throttle.commit_search_attempt(show, count_budget=not was_cached, context=ThrottleManager.CONTEXT_SEARCH_MATCH)

        matches = _validate_matches(response, items, episodes, show)
        if matches:
            throttle.record_search_success(show, context=ThrottleManager.CONTEXT_SEARCH_MATCH)
            _record_feedback(show, items, matches, response)

        return matches

    except Exception as error:
        logger.error(f"AI search matching failed: {error}")
        # Release the reservation on error so a failed attempt doesn't consume the cooldown.
        throttle.release_search_reservation(show, context=ThrottleManager.CONTEXT_SEARCH_MATCH)
        return []


def _validate_matches(
    response: Dict[str, Any],
    items: List[Dict[str, Any]],
    episodes: List["TVEpisode"],
    show: "TVShow",
) -> List[Dict[str, Any]]:
    """
    Validate the AI response against the releases we sent and the wanted episodes.

    Each accepted mapping must:
      * reference a release index we actually sent,
      * name a (season, episode) that is one of the wanted episodes,
      * meet the per-show/global confidence threshold.

    The provider still applies ``want_episode`` and downstream selection on top of this.
    """
    if not isinstance(response, dict):
        logger.warning("AI search match returned a non-dict response")
        return []

    raw_matches = response.get("matches", [])
    if not isinstance(raw_matches, list):
        logger.warning("AI search match 'matches' was not a list")
        return []

    wanted = {(episode.season, episode.episode) for episode in episodes}
    threshold = get_preferences_manager().get_confidence_threshold(show)

    accepted: List[Dict[str, Any]] = []
    used_indexes = set()
    for match in raw_matches:
        if not isinstance(match, dict):
            continue

        index = match.get("index")
        season = match.get("season")
        episode = match.get("episode")
        confidence = match.get("confidence")
        reasoning = match.get("reasoning", "No reasoning provided")

        # Strict types: bool is an int subclass in Python, so exclude it explicitly.
        if not _is_int(index) or index < 0 or index >= len(items):
            logger.debug(f"AI search match: rejecting out-of-range index {index}")
            continue
        if index in used_indexes:
            logger.debug(f"AI search match: duplicate index {index}, skipping")
            continue
        if not _is_int(season) or not _is_int(episode):
            logger.debug(f"AI search match: rejecting non-integer season/episode ({season}/{episode})")
            continue
        if (season, episode) not in wanted:
            logger.debug(f"AI search match: rejecting S{season}E{episode} (not a wanted episode of {show.name})")
            continue
        # Confidence must be an explicit, finite number in [0, 1] (no missing/bool/NaN/out-of-range).
        if not _is_valid_confidence(confidence):
            logger.debug(f"AI search match: rejecting invalid confidence {confidence!r} for S{season}E{episode}")
            continue
        if confidence < threshold:
            logger.info(f"AI search match for {show.name} S{season}E{episode} below confidence {threshold:.2f}: {confidence}")
            continue

        used_indexes.add(index)
        record = dict(items[index])
        record.update({"season": season, "episode": episode, "confidence": float(confidence), "reasoning": reasoning})
        accepted.append(record)
        logger.info(f"AI matched release '{record.get('title')}' -> {show.name} S{season}E{episode} (confidence {confidence:.2f})")

    return accepted


def _record_feedback(
    show: "TVShow",
    items: List[Dict[str, Any]],
    matches: List[Dict[str, Any]],
    response: Dict[str, Any],
) -> None:
    """Record the AI decision for feedback tracking (best effort)."""
    try:
        from sickchill.oldbeard.ai.feedback import DecisionType

        feedback_mgr = get_feedback_manager()
        summary = "; ".join(f"{m.get('title', '')[:60]} -> S{m['season']}E{m['episode']}" for m in matches[:5])
        avg_conf = sum(m["confidence"] for m in matches) / len(matches)
        feedback_mgr.record_decision(
            decision_type=DecisionType.SEARCH_MATCH,
            input_summary=f"{len(items)} unmatched releases for {show.name}",
            output_summary=f"Matched {len(matches)}: {summary}",
            confidence=avg_conf,
            reasoning=f"{len(matches)} confident mapping(s)",
            raw_response=response,
            show_id=show.indexerid,
        )
    except Exception as error:
        logger.debug(f"Failed to record AI search-match decision: {error}")
