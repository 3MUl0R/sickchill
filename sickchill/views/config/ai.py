"""
AI Configuration view handler for SickChill.

Provides the web interface for configuring AI features including
API key, model selection, search/post-processing settings, and usage monitoring.
"""

import html

from tornado.web import addslash

import sickchill.start
from sickchill import logger, settings
from sickchill.helper import try_int
from sickchill.oldbeard import config, filters, ui
from sickchill.views.common import PageTemplate
from sickchill.views.config.index import Config
from sickchill.views.routes import Route


def _try_float(value, default, min_val=None, max_val=None):
    """
    Safely convert a value to float with optional clamping.

    Args:
        value: The value to convert
        default: Default value if conversion fails
        min_val: Optional minimum value (clamp)
        max_val: Optional maximum value (clamp)

    Returns:
        Float value, clamped to range if specified
    """
    try:
        result = float(value) if value else default
    except (ValueError, TypeError):
        result = default

    if min_val is not None:
        result = max(min_val, result)
    if max_val is not None:
        result = min(max_val, result)

    return result


@Route("/config/ai(/?.*)", name="config:ai")
class ConfigAI(Config):
    @addslash
    def index(self):
        """Render the AI configuration page."""
        t = PageTemplate(rh=self, filename="config_ai.mako")

        # Get usage statistics for dashboard
        usage_stats = self._get_usage_stats()

        return t.render(
            submenu=self.ConfigMenu(),
            title=_("Config - AI Assistant"),
            header=_("AI Assistant"),
            topmenu="config",
            controller="config",
            action="ai",
            usage_stats=usage_stats,
        )

    def _get_usage_stats(self):
        """Get AI usage statistics for the dashboard."""
        try:
            from sickchill.oldbeard.ai.cost_tracker import get_cost_tracker

            tracker = get_cost_tracker()

            # Get summaries for different periods
            daily_summary = tracker.get_usage_summary("day")
            weekly_summary = tracker.get_usage_summary("week")
            monthly_summary = tracker.get_usage_summary("month")

            # Get recent usage records
            recent_usage = tracker.get_recent_usage(limit=10)

            return {
                "daily": daily_summary,
                "weekly": weekly_summary,
                "monthly": monthly_summary,
                "recent": recent_usage,
            }
        except Exception as e:
            logger.debug(f"Failed to get AI usage stats: {e}")
            return None

    def saveAI(self):
        """Save AI configuration settings."""
        # Capture old values BEFORE updating any settings (for client reset check)
        old_api_key = settings.ANTHROPIC_API_KEY
        old_model = settings.ANTHROPIC_MODEL
        old_timeout = settings.AI_REQUEST_TIMEOUT
        old_provider = settings.AI_PROVIDER
        old_cli_path = settings.AI_CLI_PATH
        old_cli_model = settings.AI_CLI_MODEL

        # Master AI settings
        settings.AI_ENABLED = config.checkbox_to_value(self.get_body_argument("ai_enabled", default=None))
        settings.AI_REQUEST_TIMEOUT = try_int(self.get_body_argument("ai_request_timeout", default=30), 30)
        settings.AI_CONFIDENCE_THRESHOLD = _try_float(
            self.get_body_argument("ai_confidence_threshold", default="0.80"),
            default=0.80,
            min_val=0.0,
            max_val=1.0,
        )
        settings.AI_NOTIFY_ON_FALLBACK_FAILURE = config.checkbox_to_value(self.get_body_argument("ai_notify_on_fallback_failure", default=None))

        # Provider selection
        provider = self.get_body_argument("ai_provider", default="api")
        settings.AI_PROVIDER = provider if provider in ("api", "cli") else "api"

        # Anthropic API settings - use unhide() to handle the hidden_value placeholder.
        # Never let an absent OR empty submission (e.g. the API panel hidden under the CLI
        # provider) null a stored key: unhide("") returns None, so only overwrite when the
        # resolved value is truthy (a real new key, or the placeholder resolving to old).
        posted_api_key = self.get_body_argument("anthropic_api_key", default="")
        resolved_api_key = filters.unhide(old_api_key, posted_api_key)
        if resolved_api_key:
            settings.ANTHROPIC_API_KEY = resolved_api_key
        settings.ANTHROPIC_MODEL = self.get_body_argument("anthropic_model", default="claude-sonnet-4-6")

        # Claude Code CLI provider settings. Only consume the path when the CLI panel is
        # present (provider == "cli"): absent -> keep stored path, empty -> clear to auto-detect.
        if settings.AI_PROVIDER == "cli":
            posted_cli_path = self.get_body_argument("ai_cli_path", default=None)
            if posted_cli_path is not None:
                settings.AI_CLI_PATH = posted_cli_path.strip()
        settings.AI_CLI_MODEL = self.get_body_argument("ai_cli_model", default=settings.AI_CLI_MODEL or "sonnet")

        # Reset client if any provider-affecting setting changed
        if (
            settings.AI_PROVIDER != old_provider
            or settings.ANTHROPIC_API_KEY != old_api_key
            or settings.ANTHROPIC_MODEL != old_model
            or settings.AI_REQUEST_TIMEOUT != old_timeout
            or settings.AI_CLI_PATH != old_cli_path
            or settings.AI_CLI_MODEL != old_cli_model
        ):
            from sickchill.oldbeard.ai import reset_client

            reset_client()

        # Budget/throttle settings
        settings.AI_MAX_CALLS_PER_HOUR = try_int(self.get_body_argument("ai_max_calls_per_hour", default=20), 20)
        settings.AI_MAX_CALLS_PER_DAY = try_int(self.get_body_argument("ai_max_calls_per_day", default=200), 200)
        settings.AI_CACHE_TTL_DAYS = try_int(self.get_body_argument("ai_cache_ttl_days", default=30), 30)

        # AI Search settings
        settings.AI_SEARCH_ENABLED = config.checkbox_to_value(self.get_body_argument("ai_search_enabled", default=None))
        settings.AI_SEARCH_ONLY_ON_FAILURE = config.checkbox_to_value(self.get_body_argument("ai_search_only_on_failure", default=None))
        settings.AI_SEARCH_COOLDOWN_DAYS_PER_SHOW = try_int(self.get_body_argument("ai_search_cooldown_days_per_show", default=7), 7)
        settings.AI_SEARCH_MIN_RESULTS = try_int(self.get_body_argument("ai_search_min_results", default=1), 1)
        settings.AI_SEARCH_FALLBACK_TO_RULES_ON_ERROR = config.checkbox_to_value(self.get_body_argument("ai_search_fallback_to_rules_on_error", default=None))
        settings.AI_SEARCH_ALLOW_RELAX_FILTERS = config.checkbox_to_value(self.get_body_argument("ai_search_allow_relax_filters", default=None))

        # AI Post-Processing Match settings
        settings.AI_POSTPROCESS_MATCH_ENABLED = config.checkbox_to_value(self.get_body_argument("ai_postprocess_match_enabled", default=None))
        settings.AI_POSTPROCESS_MATCH_ONLY_ON_FAILURE = config.checkbox_to_value(self.get_body_argument("ai_postprocess_match_only_on_failure", default=None))
        settings.AI_POSTPROCESS_MATCH_COOLDOWN_HOURS_PER_FILE = try_int(self.get_body_argument("ai_postprocess_match_cooldown_hours_per_file", default=72), 72)
        settings.AI_POSTPROCESS_MATCH_MIN_CONFIDENCE = _try_float(
            self.get_body_argument("ai_postprocess_match_min_confidence", default="0.85"),
            default=0.85,
            min_val=0.0,
            max_val=1.0,
        )

        # AI Post-Processing Analysis settings
        settings.AI_POSTPROCESS_ANALYZE_ENABLED = config.checkbox_to_value(self.get_body_argument("ai_postprocess_analyze_enabled", default=None))
        settings.AI_POSTPROCESS_VERIFY_QUALITY = config.checkbox_to_value(self.get_body_argument("ai_postprocess_verify_quality", default=None))
        settings.AI_POSTPROCESS_DETECT_ISSUES = config.checkbox_to_value(self.get_body_argument("ai_postprocess_detect_issues", default=None))
        settings.AI_POSTPROCESS_SUGGEST_METADATA = config.checkbox_to_value(self.get_body_argument("ai_postprocess_suggest_metadata", default=None))

        # Save config
        sickchill.start.save_config()

        ui.notifications.message(_("Configuration Saved"), _("AI configuration has been saved."))
        return self.redirect("/config/ai/")

    def testAnthropicKey(self):
        """Test the Anthropic API key connection."""
        api_key = self.get_body_argument("api_key", default="")

        # Use unhide to handle hidden_value placeholder
        api_key = filters.unhide(settings.ANTHROPIC_API_KEY, api_key)

        if not api_key:
            return "No API key provided"

        try:
            from sickchill.oldbeard.ai.anthropic_client import AnthropicClient

            # Create a temporary client with the test key
            model = self.get_body_argument("model", default=settings.ANTHROPIC_MODEL)
            client = AnthropicClient(api_key=api_key, model=model, timeout=15)

            success, message = client.test_connection()

            # Escape the message to prevent XSS
            safe_message = html.escape(str(message))

            if success:
                return f"Success: {safe_message}"
            else:
                return f"Failed: {safe_message}"

        except Exception as e:
            logger.warning(f"API key test failed: {e}")
            # Escape exception text to prevent XSS
            safe_error = html.escape(str(e))
            return f"Error: {safe_error}"

    def testClaudeCLI(self):
        """Detect and test the local Claude Code CLI provider (binary + login + round-trip)."""
        try:
            from sickchill.oldbeard.ai.cli_client import ClaudeCLIClient

            cli_path = self.get_body_argument("cli_path", default=settings.AI_CLI_PATH) or ""
            model = self.get_body_argument("model", default=settings.AI_CLI_MODEL) or "sonnet"

            client = ClaudeCLIClient(model=model, timeout=max(int(settings.AI_REQUEST_TIMEOUT), 30), cli_path=cli_path.strip())
            success, message = client.test_connection()

            # Escape the message to prevent XSS
            safe_message = html.escape(str(message))
            return f"Success: {safe_message}" if success else f"Failed: {safe_message}"

        except Exception as e:
            logger.warning(f"Claude CLI test failed: {e}")
            safe_error = html.escape(str(e))
            return f"Error: {safe_error}"

    def getUsageStats(self):
        """Get AI usage statistics as JSON."""
        import json

        stats = self._get_usage_stats()
        if stats:
            # Convert dataclasses to dicts for JSON serialization
            return json.dumps(
                {
                    "daily": {
                        "requests": stats["daily"].total_requests,
                        "cost": f"${stats['daily'].total_estimated_cost_usd:.4f}",
                    },
                    "weekly": {
                        "requests": stats["weekly"].total_requests,
                        "cost": f"${stats['weekly'].total_estimated_cost_usd:.4f}",
                    },
                    "monthly": {
                        "requests": stats["monthly"].total_requests,
                        "cost": f"${stats['monthly'].total_estimated_cost_usd:.4f}",
                    },
                }
            )
        return json.dumps({"error": "Unable to get usage stats"})
