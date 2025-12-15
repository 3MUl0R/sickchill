"""
AI Cost Tracking for SickChill.

Tracks API usage, token counts, and estimated costs per request.
Provides summaries for cost visibility and budgeting.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sickchill import settings
from sickchill.oldbeard import db

logger = logging.getLogger(__name__)


# Pricing per million tokens (as of 2025)
# These are approximate and may change - users should verify with Anthropic
MODEL_PRICING = {
    "claude-sonnet-4-20250514": {
        "input_per_million": 3.00,
        "output_per_million": 15.00,
    },
    "claude-haiku-3-5-20241022": {
        "input_per_million": 0.25,
        "output_per_million": 1.25,
    },
}

# Default pricing for unknown models
DEFAULT_PRICING = {
    "input_per_million": 3.00,
    "output_per_million": 15.00,
}


@dataclass
class UsageRecord:
    """Record of a single API request's usage."""

    timestamp: float
    model: str
    context: str  # "search" or "postprocess"
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    scope_key: Optional[str] = None  # show ID or file fingerprint


@dataclass
class UsageSummary:
    """Summary of usage over a time period."""

    period: str  # "hour", "day", "week", "month", "all"
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost_usd: float
    by_context: Dict[str, Dict[str, Any]]
    by_model: Dict[str, Dict[str, Any]]


class CostTracker:
    """
    Tracks AI API usage and costs.

    Uses cache.db to persist usage records for cost analysis.
    """

    def __init__(self):
        """Initialize the cost tracker."""
        self._ensure_table()

    def _get_db(self) -> db.DBConnection:
        """Get a database connection to cache.db."""
        return db.DBConnection("cache.db")

    def _ensure_table(self) -> None:
        """Ensure the cost tracking table exists."""
        cache_db = self._get_db()

        if not cache_db.has_table("ai_usage"):
            logger.info("Creating ai_usage table")
            cache_db.action(
                """
                CREATE TABLE ai_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp NUMERIC NOT NULL,
                    model TEXT NOT NULL,
                    context TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    estimated_cost_usd REAL NOT NULL,
                    scope_key TEXT
                )
                """
            )
            cache_db.action("CREATE INDEX IF NOT EXISTS idx_ai_usage_timestamp ON ai_usage (timestamp)")
            cache_db.action("CREATE INDEX IF NOT EXISTS idx_ai_usage_context ON ai_usage (context)")

    def record_usage(
        self,
        model: str,
        context: str,
        input_tokens: int,
        output_tokens: int,
        scope_key: Optional[str] = None,
    ) -> UsageRecord:
        """
        Record API usage from a request.

        Args:
            model: The model used (e.g., "claude-sonnet-4-20250514")
            context: The context ("search" or "postprocess")
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens used
            scope_key: Optional show ID or file fingerprint

        Returns:
            UsageRecord with the recorded data including estimated cost
        """
        timestamp = time.time()

        # Calculate estimated cost
        pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
        input_cost = (input_tokens / 1_000_000) * pricing["input_per_million"]
        output_cost = (output_tokens / 1_000_000) * pricing["output_per_million"]
        estimated_cost = input_cost + output_cost

        # Store in database
        cache_db = self._get_db()
        cache_db.action(
            """
            INSERT INTO ai_usage (timestamp, model, context, input_tokens, output_tokens, estimated_cost_usd, scope_key)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [timestamp, model, context, input_tokens, output_tokens, estimated_cost, scope_key],
        )

        record = UsageRecord(
            timestamp=timestamp,
            model=model,
            context=context,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost,
            scope_key=scope_key,
        )

        logger.debug(
            f"AI usage recorded: {context} - {input_tokens} in, {output_tokens} out, "
            f"${estimated_cost:.6f} estimated"
        )

        return record

    def get_usage_summary(self, period: str = "day") -> UsageSummary:
        """
        Get a summary of usage over a time period.

        Args:
            period: One of "hour", "day", "week", "month", "all"

        Returns:
            UsageSummary with aggregated data
        """
        now = time.time()

        # Calculate cutoff time
        period_seconds = {
            "hour": 3600,
            "day": 86400,
            "week": 604800,
            "month": 2592000,  # 30 days
            "all": 0,
        }

        if period not in period_seconds:
            period = "day"

        cutoff = 0 if period == "all" else now - period_seconds[period]

        cache_db = self._get_db()

        # Get all records for the period
        if period == "all":
            results = cache_db.select(
                "SELECT * FROM ai_usage ORDER BY timestamp DESC"
            )
        else:
            results = cache_db.select(
                "SELECT * FROM ai_usage WHERE timestamp > ? ORDER BY timestamp DESC",
                [cutoff],
            )

        if not results:
            return UsageSummary(
                period=period,
                total_requests=0,
                total_input_tokens=0,
                total_output_tokens=0,
                total_estimated_cost_usd=0.0,
                by_context={},
                by_model={},
            )

        # Aggregate data
        total_requests = len(results)
        total_input_tokens = sum(r["input_tokens"] for r in results)
        total_output_tokens = sum(r["output_tokens"] for r in results)
        total_cost = sum(r["estimated_cost_usd"] for r in results)

        # Group by context
        by_context: Dict[str, Dict[str, Any]] = {}
        for r in results:
            ctx = r["context"]
            if ctx not in by_context:
                by_context[ctx] = {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost_usd": 0.0,
                }
            by_context[ctx]["requests"] += 1
            by_context[ctx]["input_tokens"] += r["input_tokens"]
            by_context[ctx]["output_tokens"] += r["output_tokens"]
            by_context[ctx]["estimated_cost_usd"] += r["estimated_cost_usd"]

        # Group by model
        by_model: Dict[str, Dict[str, Any]] = {}
        for r in results:
            model = r["model"]
            if model not in by_model:
                by_model[model] = {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost_usd": 0.0,
                }
            by_model[model]["requests"] += 1
            by_model[model]["input_tokens"] += r["input_tokens"]
            by_model[model]["output_tokens"] += r["output_tokens"]
            by_model[model]["estimated_cost_usd"] += r["estimated_cost_usd"]

        return UsageSummary(
            period=period,
            total_requests=total_requests,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_estimated_cost_usd=total_cost,
            by_context=by_context,
            by_model=by_model,
        )

    def get_recent_usage(self, limit: int = 20) -> List[UsageRecord]:
        """
        Get the most recent usage records.

        Args:
            limit: Maximum number of records to return

        Returns:
            List of UsageRecord objects, most recent first
        """
        cache_db = self._get_db()
        results = cache_db.select(
            "SELECT * FROM ai_usage ORDER BY timestamp DESC LIMIT ?",
            [limit],
        )

        return [
            UsageRecord(
                timestamp=r["timestamp"],
                model=r["model"],
                context=r["context"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                estimated_cost_usd=r["estimated_cost_usd"],
                scope_key=r["scope_key"],
            )
            for r in results
        ]

    def get_usage_for_show(self, show_id: int) -> Dict[str, Any]:
        """
        Get usage statistics for a specific show.

        Args:
            show_id: The show's indexer ID

        Returns:
            Dict with usage stats for the show
        """
        cache_db = self._get_db()
        scope_key = str(show_id)

        results = cache_db.select(
            """
            SELECT COUNT(*) as requests,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(estimated_cost_usd) as estimated_cost_usd
            FROM ai_usage WHERE scope_key = ?
            """,
            [scope_key],
        )

        if results and results[0]["requests"]:
            return {
                "requests": results[0]["requests"],
                "input_tokens": results[0]["input_tokens"] or 0,
                "output_tokens": results[0]["output_tokens"] or 0,
                "estimated_cost_usd": results[0]["estimated_cost_usd"] or 0.0,
            }

        return {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

    def cleanup_old_records(self, days: int = 90) -> int:
        """
        Remove usage records older than the specified number of days.

        Args:
            days: Number of days to keep records

        Returns:
            Number of records deleted
        """
        cache_db = self._get_db()
        cutoff = time.time() - (days * 86400)

        # Get count before deletion
        result = cache_db.select(
            "SELECT COUNT(*) as count FROM ai_usage WHERE timestamp < ?",
            [cutoff],
        )
        count = result[0]["count"] if result else 0

        if count > 0:
            cache_db.action("DELETE FROM ai_usage WHERE timestamp < ?", [cutoff])
            logger.info(f"Cleaned up {count} AI usage records older than {days} days")

        return count

    def format_summary(self, summary: UsageSummary) -> str:
        """
        Format a usage summary as a human-readable string.

        Args:
            summary: The UsageSummary to format

        Returns:
            Formatted string
        """
        lines = [
            f"AI Usage Summary ({summary.period})",
            f"  Total requests: {summary.total_requests}",
            f"  Total tokens: {summary.total_input_tokens:,} input, {summary.total_output_tokens:,} output",
            f"  Estimated cost: ${summary.total_estimated_cost_usd:.4f} USD",
        ]

        if summary.by_context:
            lines.append("  By context:")
            for ctx, data in summary.by_context.items():
                lines.append(
                    f"    {ctx}: {data['requests']} requests, ${data['estimated_cost_usd']:.4f}"
                )

        if summary.by_model:
            lines.append("  By model:")
            for model, data in summary.by_model.items():
                lines.append(
                    f"    {model}: {data['requests']} requests, ${data['estimated_cost_usd']:.4f}"
                )

        return "\n".join(lines)


# Module-level singleton
_cost_tracker: Optional[CostTracker] = None


def get_cost_tracker() -> CostTracker:
    """Get the global CostTracker instance."""
    global _cost_tracker
    if _cost_tracker is None:
        _cost_tracker = CostTracker()
    return _cost_tracker
