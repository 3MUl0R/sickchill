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
from sickchill.oldbeard.name_parser.parser import franchise_gate

if TYPE_CHECKING:
    from sickchill.tv import TVEpisode, TVShow

logger = logging.getLogger(__name__)

# Cap on how many dropped releases we hand to the AI in one request (keeps the prompt and
# response bounded; the throttle already bounds how often we call at all). Applied AFTER de-duping,
# since the same release URL is returned by many overlapping per-episode/free-text queries.
# Effort-dependent: higher reasoning effort generates far more thinking tokens per release (slower,
# closer to the request timeout), so we hand it fewer releases. Read at call time so a settings
# change takes effect without reimport.
_HIGH_EFFORTS = ("high", "xhigh", "max")
MAX_RELEASES_HIGH_EFFORT = 20
MAX_RELEASES_DEFAULT = 30


def _max_releases_per_request() -> int:
    return MAX_RELEASES_HIGH_EFFORT if getattr(settings, "AI_CLI_EFFORT", "low") in _HIGH_EFFORTS else MAX_RELEASES_DEFAULT

# Cooldown for an AUTOMATIC search-match attempt, scoped per (show, provider, search_mode, season).
# Short by design: a backlog runs one season at a time, so the matcher must be free to fire for every
# season (and re-attempt on later cycles to catch newly-available releases). This is independent of
# the 7-day per-show advisor cooldown. User-triggered manual searches bypass it entirely.
SEARCH_MATCH_COOLDOWN_HOURS = 6


def _normalize_title(title: str) -> str:
    """Lowercase and strip non-alphanumerics for a stable dedupe key."""
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())


def _dedupe_unmatched(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """De-duplicate dropped releases, preserving order.

    The same release is returned by many overlapping queries (per-episode structured + free-text),
    so ``unmatched_items`` is heavily duplicated before we cap it. Key on the download ``url`` when
    present (SickChill treats result URLs as unique), else fall back to ``(normalized_title, size)``
    so distinct URL-less rows are not collapsed together.
    """
    seen = set()
    unique: List[Dict[str, Any]] = []
    for item in items:
        url = item.get("url")
        if url:
            key = ("url", url)
        else:
            key = ("title", _normalize_title(item.get("title", "")), item.get("size", -1) or -1)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


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


def _build_season_structure(show: "TVShow", max_rows: int = 20) -> str:
    """
    A compact per-season table (episode count + absolute range) so the AI can see the franchise
    structure -- e.g. that "Gintama (2015)" content lives in a specific later season with its own
    absolute range, not at absolute 50. Best effort; returns "unknown" on any problem.
    """
    try:
        from sickchill.oldbeard import db

        main_db_con = db.DBConnection()
        rows = main_db_con.select(
            "SELECT season, COUNT(*) AS episodes, MIN(absolute_number) AS abs_min, MAX(absolute_number) AS abs_max "
            "FROM tv_episodes WHERE showid = ? AND indexer = ? GROUP BY season ORDER BY season",
            [show.indexerid, show.indexer],
        )
    except Exception as error:
        logger.debug(f"Could not build the season structure for {show.name}: {error}")
        return "unknown"

    lines = []
    for row in rows[:max_rows]:
        absolutes = ""
        if row["abs_min"] or row["abs_max"]:
            absolutes = f", absolute {row['abs_min']}-{row['abs_max']}"
        lines.append(f"- season {row['season']}: {row['episodes']} episodes{absolutes}")
    if len(rows) > max_rows:
        lines.append(f"- ... {len(rows) - max_rows} more seasons")
    return "\n".join(lines) if lines else "unknown"


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
    *,
    provider_id: str = "",
    search_mode: str = "episode",
    manual_search: bool = False,
    is_failed_retry: bool = False,
) -> List[Dict[str, Any]]:
    """
    Ask the AI to map dropped anime releases to wanted episodes of ``show``.

    Args:
        show: The show currently being searched.
        episodes: The wanted episodes being searched for (the only valid targets).
        unmatched_items: Dropped releases. Each is a dict with at least ``title`` and the raw
            provider ``item``; ``url``/``size``/``seeders``/``leechers`` are passed through.
        provider_id: Stable provider id; part of the cooldown scope so each provider attempts
            independently within one search.
        search_mode: "episode" or "season"; part of the cooldown scope so the season->episode
            fallback within one search is not suppressed.
        manual_search: True for a user-triggered manual search. Such searches bypass the cooldown
            (the user asked for it now); the global budget and concurrency guard still apply.
        is_failed_retry: True for a post-failed-download retry. These arrive with manual_search=True
            but should stay throttled, so they do NOT bypass the cooldown.

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

    # Cooldown scope: per (show, provider, search_mode, season-set). Finer than per-show so a single
    # backlog pass can match every season (one BacklogQueueItem per season) and the provider/mode
    # fallbacks within one search are not suppressed by the first attempt's cooldown.
    seasons = "-".join(sorted({str(getattr(episode, "season", "")) for episode in episodes}))
    scope_key = f"{show.indexerid}:{provider_id}:{search_mode}:s{seasons}"
    # Manual (but NOT failed-retry) searches bypass the cooldown; everything else uses the short one.
    bypass_cooldown = bool(manual_search) and not bool(is_failed_retry)
    cooldown_seconds = 0 if bypass_cooldown else SEARCH_MATCH_COOLDOWN_HOURS * 60 * 60

    throttle = get_throttle()
    if not throttle.reserve_search_attempt(
        show, context=ThrottleManager.CONTEXT_SEARCH_MATCH, scope_key=scope_key, cooldown_seconds=cooldown_seconds
    ):
        logger.debug(f"AI search matching for {show.name} blocked by cooldown or concurrent request ({scope_key})")
        return []

    # De-dupe before capping so the cap holds diverse releases, not duplicates of a few episodes.
    deduped = _dedupe_unmatched(unmatched_items)
    cap = _max_releases_per_request()
    items = deduped[:cap]
    if len(unmatched_items) != len(deduped):
        logger.debug(f"AI search matching: de-duped {len(unmatched_items)} dropped releases to {len(deduped)} for {show.name}")
    if len(deduped) > len(items):
        logger.warning(
            f"AI search matching: {len(deduped)} unique releases exceeds cap {cap} for {show.name}; "
            f"{len(deduped) - len(items)} not sent to AI this pass"
        )

    try:
        template = _load_prompt_template()
        aliases = _get_aliases(show)
        # The per-match "reasoning" prose is diagnostic-only and dominates output size; only request
        # it when explicitly enabled. The comma lives inside the substituted value so the JSON example
        # stays valid when reasoning is omitted.
        reasoning_field = ',\n      "reasoning": "<brief explanation>"' if settings.AI_SEARCH_MATCH_INCLUDE_REASONING else ""
        prompt = template.format(
            show_name=show.name,
            startyear=getattr(show, "startyear", None) or "unknown",
            season_structure=_build_season_structure(show),
            aliases=", ".join(aliases) if aliases else "None",
            wanted_json=_build_wanted_json(episodes),
            releases_json=_build_releases_json(items),
            reasoning_field=reasoning_field,
        )

        client = get_client()
        if not client:
            logger.warning("AI client not available")
            throttle.release_search_reservation(show, context=ThrottleManager.CONTEXT_SEARCH_MATCH, scope_key=scope_key)
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
        throttle.commit_search_attempt(show, count_budget=not was_cached, context=ThrottleManager.CONTEXT_SEARCH_MATCH, scope_key=scope_key)

        matches = _validate_matches(response, items, episodes, show)
        if matches:
            throttle.record_search_success(show, context=ThrottleManager.CONTEXT_SEARCH_MATCH, scope_key=scope_key)
            _record_feedback(show, items, matches, response)

        return matches

    except Exception as error:
        logger.error(f"AI search matching failed: {error}")
        # Release the reservation on error so a failed attempt doesn't consume the cooldown.
        throttle.release_search_reservation(show, context=ThrottleManager.CONTEXT_SEARCH_MATCH, scope_key=scope_key)
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
        # Franchise gate (deterministic, reject-only): the prompt's franchise guidance is advice,
        # THIS is the safety boundary. A title carrying a year/alias/sequel-marker/season-token
        # the proposed season cannot explain is rejected no matter how confident the AI is --
        # this is exactly how "[Abystoma] Gintama (2015)-50" was twice mapped to classic S2E1.
        title = items[index].get("title") or ""
        matched_episode = next((ep for ep in episodes if ep.season == season and ep.episode == episode), None)
        allowed, reason = franchise_gate(title, show, season, scene_season=getattr(matched_episode, "scene_season", None))
        if not allowed:
            logger.info(f"AI search match for {show.name}: rejecting '{title}' -> S{season}E{episode}: {reason}")
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
