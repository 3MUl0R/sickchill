"""
AI Feedback Loop for SickChill.

Allows users to provide feedback on AI decisions (correct/incorrect),
which can be used to track AI performance and potentially improve
future decisions.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from sickchill.oldbeard import db

logger = logging.getLogger(__name__)


class FeedbackType(Enum):
    """Types of feedback that can be provided."""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    PARTIALLY_CORRECT = "partially_correct"


class DecisionType(Enum):
    """Types of AI decisions that can receive feedback."""

    SEARCH_SELECTION = "search_selection"
    FILE_MATCH = "file_match"
    FILE_ANALYSIS = "file_analysis"


@dataclass
class AIDecision:
    """Record of an AI decision for tracking."""

    decision_id: str
    decision_type: str
    timestamp: float
    show_id: Optional[int]
    input_summary: str  # Brief summary of input (e.g., "10 search results for Show X S01E05")
    output_summary: str  # Brief summary of output (e.g., "Selected result #3: Release.Name")
    confidence: float
    reasoning: str
    raw_response: Dict[str, Any]


@dataclass
class AIFeedback:
    """User feedback on an AI decision."""

    decision_id: str
    feedback_type: str
    user_correction: Optional[str]  # What the correct answer should have been
    notes: Optional[str]  # Additional user notes
    timestamp: float


class FeedbackManager:
    """
    Manages AI decision tracking and user feedback.

    Stores decisions and feedback in cache.db for analysis.
    """

    def __init__(self):
        """Initialize the feedback manager."""
        self._ensure_tables()

    def _get_db(self) -> db.DBConnection:
        """Get a database connection to cache.db."""
        return db.DBConnection("cache.db")

    def _ensure_tables(self) -> None:
        """Ensure the feedback tables exist."""
        cache_db = self._get_db()

        if not cache_db.has_table("ai_decisions"):
            logger.info("Creating ai_decisions table")
            cache_db.action(
                """
                CREATE TABLE ai_decisions (
                    decision_id TEXT PRIMARY KEY,
                    decision_type TEXT NOT NULL,
                    timestamp NUMERIC NOT NULL,
                    show_id INTEGER,
                    input_summary TEXT NOT NULL,
                    output_summary TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reasoning TEXT,
                    raw_response TEXT
                )
                """
            )
            cache_db.action("CREATE INDEX IF NOT EXISTS idx_ai_decisions_timestamp ON ai_decisions (timestamp)")
            cache_db.action("CREATE INDEX IF NOT EXISTS idx_ai_decisions_show ON ai_decisions (show_id)")

        if not cache_db.has_table("ai_feedback"):
            logger.info("Creating ai_feedback table")
            cache_db.action(
                """
                CREATE TABLE ai_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL,
                    feedback_type TEXT NOT NULL,
                    user_correction TEXT,
                    notes TEXT,
                    timestamp NUMERIC NOT NULL,
                    FOREIGN KEY (decision_id) REFERENCES ai_decisions (decision_id)
                )
                """
            )
            cache_db.action("CREATE INDEX IF NOT EXISTS idx_ai_feedback_decision ON ai_feedback (decision_id)")

    def record_decision(
        self,
        decision_type: DecisionType,
        input_summary: str,
        output_summary: str,
        confidence: float,
        reasoning: str,
        raw_response: Dict[str, Any],
        show_id: Optional[int] = None,
    ) -> str:
        """
        Record an AI decision for tracking.

        Args:
            decision_type: Type of decision (search, match, analysis)
            input_summary: Brief description of the input
            output_summary: Brief description of the output/result
            confidence: AI's confidence in the decision
            reasoning: AI's reasoning for the decision
            raw_response: The raw AI response dict
            show_id: Optional show ID

        Returns:
            The decision_id for later feedback
        """
        import hashlib

        # Generate a unique decision ID
        timestamp = time.time()
        hash_input = f"{decision_type.value}:{timestamp}:{input_summary}:{output_summary}"
        decision_id = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

        cache_db = self._get_db()
        cache_db.action(
            """
            INSERT INTO ai_decisions
            (decision_id, decision_type, timestamp, show_id, input_summary, output_summary, confidence, reasoning, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                decision_id,
                decision_type.value,
                timestamp,
                show_id,
                input_summary[:500],  # Truncate for DB
                output_summary[:500],
                confidence,
                reasoning[:1000] if reasoning else None,
                json.dumps(raw_response),
            ],
        )

        logger.debug(f"Recorded AI decision {decision_id}: {decision_type.value}")
        return decision_id

    def record_feedback(
        self,
        decision_id: str,
        feedback_type: FeedbackType,
        user_correction: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> bool:
        """
        Record user feedback on an AI decision.

        Args:
            decision_id: The decision to provide feedback on
            feedback_type: Whether the decision was correct/incorrect
            user_correction: What the correct answer should have been
            notes: Additional user notes

        Returns:
            True if feedback was recorded successfully
        """
        cache_db = self._get_db()

        # Verify decision exists
        result = cache_db.select(
            "SELECT decision_id FROM ai_decisions WHERE decision_id = ?",
            [decision_id],
        )
        if not result:
            logger.warning(f"Decision {decision_id} not found for feedback")
            return False

        cache_db.action(
            """
            INSERT INTO ai_feedback (decision_id, feedback_type, user_correction, notes, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                decision_id,
                feedback_type.value,
                user_correction[:500] if user_correction else None,
                notes[:500] if notes else None,
                time.time(),
            ],
        )

        logger.info(f"Recorded {feedback_type.value} feedback for decision {decision_id}")
        return True

    def get_decision(self, decision_id: str) -> Optional[AIDecision]:
        """
        Get a decision by ID.

        Args:
            decision_id: The decision ID

        Returns:
            AIDecision object or None
        """
        cache_db = self._get_db()
        result = cache_db.select(
            "SELECT * FROM ai_decisions WHERE decision_id = ?",
            [decision_id],
        )

        if not result:
            return None

        row = result[0]
        return AIDecision(
            decision_id=row["decision_id"],
            decision_type=row["decision_type"],
            timestamp=row["timestamp"],
            show_id=row["show_id"],
            input_summary=row["input_summary"],
            output_summary=row["output_summary"],
            confidence=row["confidence"],
            reasoning=row["reasoning"] or "",
            raw_response=json.loads(row["raw_response"]) if row["raw_response"] else {},
        )

    def get_recent_decisions(
        self,
        limit: int = 50,
        decision_type: Optional[DecisionType] = None,
        show_id: Optional[int] = None,
    ) -> List[AIDecision]:
        """
        Get recent AI decisions.

        Args:
            limit: Maximum number to return
            decision_type: Filter by decision type
            show_id: Filter by show ID

        Returns:
            List of AIDecision objects, most recent first
        """
        cache_db = self._get_db()

        query = "SELECT * FROM ai_decisions"
        params: List[Any] = []
        conditions: List[str] = []

        if decision_type:
            conditions.append("decision_type = ?")
            params.append(decision_type.value)

        if show_id is not None:
            conditions.append("show_id = ?")
            params.append(show_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        results = cache_db.select(query, params)

        return [
            AIDecision(
                decision_id=row["decision_id"],
                decision_type=row["decision_type"],
                timestamp=row["timestamp"],
                show_id=row["show_id"],
                input_summary=row["input_summary"],
                output_summary=row["output_summary"],
                confidence=row["confidence"],
                reasoning=row["reasoning"] or "",
                raw_response=json.loads(row["raw_response"]) if row["raw_response"] else {},
            )
            for row in results
        ]

    def get_feedback_for_decision(self, decision_id: str) -> List[AIFeedback]:
        """
        Get all feedback for a decision.

        Args:
            decision_id: The decision ID

        Returns:
            List of AIFeedback objects
        """
        cache_db = self._get_db()
        results = cache_db.select(
            "SELECT * FROM ai_feedback WHERE decision_id = ? ORDER BY timestamp DESC",
            [decision_id],
        )

        return [
            AIFeedback(
                decision_id=row["decision_id"],
                feedback_type=row["feedback_type"],
                user_correction=row["user_correction"],
                notes=row["notes"],
                timestamp=row["timestamp"],
            )
            for row in results
        ]

    def get_feedback_stats(
        self,
        days: int = 30,
        decision_type: Optional[DecisionType] = None,
    ) -> Dict[str, Any]:
        """
        Get feedback statistics for AI performance analysis.

        Args:
            days: Number of days to analyze
            decision_type: Filter by decision type

        Returns:
            Dict with feedback statistics
        """
        cache_db = self._get_db()
        cutoff = time.time() - (days * 86400)

        # Get all decisions in the period
        query = "SELECT COUNT(*) as count FROM ai_decisions WHERE timestamp > ?"
        params: List[Any] = [cutoff]
        if decision_type:
            query += " AND decision_type = ?"
            params.append(decision_type.value)

        result = cache_db.select(query, params)
        total_decisions = result[0]["count"] if result else 0

        # Get feedback counts
        query = """
            SELECT f.feedback_type, COUNT(*) as count
            FROM ai_feedback f
            JOIN ai_decisions d ON f.decision_id = d.decision_id
            WHERE d.timestamp > ?
        """
        params = [cutoff]
        if decision_type:
            query += " AND d.decision_type = ?"
            params.append(decision_type.value)
        query += " GROUP BY f.feedback_type"

        feedback_results = cache_db.select(query, params)
        feedback_counts = {row["feedback_type"]: row["count"] for row in feedback_results}

        # Calculate accuracy
        total_feedback = sum(feedback_counts.values())
        correct = feedback_counts.get(FeedbackType.CORRECT.value, 0)
        partially = feedback_counts.get(FeedbackType.PARTIALLY_CORRECT.value, 0)
        incorrect = feedback_counts.get(FeedbackType.INCORRECT.value, 0)

        accuracy = 0.0
        if total_feedback > 0:
            # Count partially correct as 0.5
            accuracy = (correct + (partially * 0.5)) / total_feedback

        return {
            "period_days": days,
            "total_decisions": total_decisions,
            "total_feedback": total_feedback,
            "correct": correct,
            "partially_correct": partially,
            "incorrect": incorrect,
            "accuracy": accuracy,
            "feedback_rate": total_feedback / total_decisions if total_decisions > 0 else 0,
        }

    def cleanup_old_records(self, days: int = 90) -> int:
        """
        Remove old decision and feedback records.

        Args:
            days: Number of days to keep

        Returns:
            Number of decisions deleted
        """
        cache_db = self._get_db()
        cutoff = time.time() - (days * 86400)

        # Get count of decisions to delete
        result = cache_db.select(
            "SELECT COUNT(*) as count FROM ai_decisions WHERE timestamp < ?",
            [cutoff],
        )
        count = result[0]["count"] if result else 0

        if count > 0:
            # Delete feedback first (foreign key)
            cache_db.action(
                """
                DELETE FROM ai_feedback WHERE decision_id IN (
                    SELECT decision_id FROM ai_decisions WHERE timestamp < ?
                )
                """,
                [cutoff],
            )
            # Delete decisions
            cache_db.action("DELETE FROM ai_decisions WHERE timestamp < ?", [cutoff])
            logger.info(f"Cleaned up {count} AI decision records older than {days} days")

        return count


# Module-level singleton
_feedback_manager: Optional[FeedbackManager] = None


def get_feedback_manager() -> FeedbackManager:
    """Get the global FeedbackManager instance."""
    global _feedback_manager
    if _feedback_manager is None:
        _feedback_manager = FeedbackManager()
    return _feedback_manager
