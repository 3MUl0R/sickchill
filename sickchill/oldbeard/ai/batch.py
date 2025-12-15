"""
Batch Processing for AI Operations in SickChill.

Provides utilities for queueing and processing multiple AI requests
efficiently with rate limiting and progress tracking.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class BatchStatus(Enum):
    """Status of a batch job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BatchItem:
    """A single item in a batch."""

    item_id: str
    data: Dict[str, Any]
    status: BatchStatus = BatchStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class BatchJob:
    """A batch job containing multiple items."""

    job_id: str
    items: List[BatchItem] = field(default_factory=list)
    status: BatchStatus = BatchStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    progress: int = 0  # Number of items processed
    _stop_event: threading.Event = field(default_factory=threading.Event)

    @property
    def total_items(self) -> int:
        """Get total number of items."""
        return len(self.items)

    @property
    def successful_items(self) -> int:
        """Get number of successfully processed items."""
        return sum(1 for item in self.items if item.status == BatchStatus.COMPLETED)

    @property
    def failed_items(self) -> int:
        """Get number of failed items."""
        return sum(1 for item in self.items if item.status == BatchStatus.FAILED)


class BatchProcessor:
    """
    Processes batches of AI operations with rate limiting.

    Designed for background processing of multiple items
    while respecting API rate limits.
    """

    def __init__(
        self,
        rate_limit_per_minute: int = 10,
        max_concurrent: int = 1,
    ):
        """
        Initialize the batch processor.

        Args:
            rate_limit_per_minute: Maximum requests per minute
            max_concurrent: Maximum concurrent processing (usually 1 for AI)
        """
        self._rate_limit = rate_limit_per_minute
        self._max_concurrent = max_concurrent
        self._jobs: Dict[str, BatchJob] = {}
        self._lock = threading.Lock()
        self._rate_limit_lock = threading.Lock()
        self._last_request_time = 0.0

    def create_job(self, items: List[Dict[str, Any]]) -> str:
        """
        Create a new batch job.

        Args:
            items: List of item data dicts to process

        Returns:
            Job ID
        """
        import hashlib

        job_id = hashlib.sha256(
            f"{time.time()}:{len(items)}".encode()
        ).hexdigest()[:12]

        batch_items = []
        for i, item_data in enumerate(items):
            item_id = f"{job_id}_{i}"
            batch_items.append(BatchItem(item_id=item_id, data=item_data))

        job = BatchJob(job_id=job_id, items=batch_items)

        with self._lock:
            self._jobs[job_id] = job

        logger.info(f"Created batch job {job_id} with {len(items)} items")
        return job_id

    def start_job(
        self,
        job_id: str,
        processor: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> bool:
        """
        Start processing a batch job.

        Args:
            job_id: The job ID to start
            processor: Function to process each item

        Returns:
            True if job started successfully
        """
        with self._lock:
            if job_id not in self._jobs:
                logger.warning(f"Job {job_id} not found")
                return False

            job = self._jobs[job_id]
            if job.status != BatchStatus.PENDING:
                logger.warning(f"Job {job_id} is not pending (status: {job.status})")
                return False

            job.status = BatchStatus.RUNNING
            job.started_at = time.time()

        # Process in background thread
        def _process():
            try:
                self._process_job(job_id, processor)
            except Exception as e:
                logger.error(f"Batch job {job_id} failed: {e}")
                with self._lock:
                    if job_id in self._jobs:
                        self._jobs[job_id].status = BatchStatus.FAILED

        thread = threading.Thread(target=_process, daemon=True)
        thread.start()

        logger.info(f"Started batch job {job_id}")
        return True

    def _process_job(
        self,
        job_id: str,
        processor: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> None:
        """
        Process all items in a job.

        Args:
            job_id: The job ID
            processor: Function to process each item
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return

        for item in job.items:
            # Check if cancelled (per-job stop event)
            if job._stop_event.is_set():
                with self._lock:
                    job.status = BatchStatus.CANCELLED
                return

            # Rate limiting
            self._wait_for_rate_limit()

            # Process item
            try:
                item.status = BatchStatus.RUNNING
                result = processor(item.data)
                item.result = result
                item.status = BatchStatus.COMPLETED
            except Exception as e:
                logger.warning(f"Item {item.item_id} failed: {e}")
                item.error = str(e)
                item.status = BatchStatus.FAILED

            with self._lock:
                job.progress += 1

        # Mark job complete
        with self._lock:
            job.status = BatchStatus.COMPLETED
            job.completed_at = time.time()

        logger.info(
            f"Batch job {job_id} completed: "
            f"{job.successful_items}/{job.total_items} successful"
        )

    def _wait_for_rate_limit(self) -> None:
        """Wait if necessary to respect rate limits."""
        min_interval = 60.0 / self._rate_limit
        wait_time = 0.0

        # Check and calculate wait time while holding lock briefly
        with self._rate_limit_lock:
            elapsed = time.time() - self._last_request_time
            if elapsed < min_interval:
                wait_time = min_interval - elapsed

        # Sleep outside the lock to avoid blocking other operations
        if wait_time > 0:
            time.sleep(wait_time)

        # Update last request time
        with self._rate_limit_lock:
            self._last_request_time = time.time()

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the status of a batch job.

        Args:
            job_id: The job ID

        Returns:
            Dict with job status info or None if not found
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None

            return {
                "job_id": job.job_id,
                "status": job.status.value,
                "total_items": job.total_items,
                "progress": job.progress,
                "successful": job.successful_items,
                "failed": job.failed_items,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
            }

    def get_job_results(self, job_id: str) -> Optional[List[Dict[str, Any]]]:
        """
        Get results of a completed batch job.

        Args:
            job_id: The job ID

        Returns:
            List of item results or None if not found/incomplete
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None

            return [
                {
                    "item_id": item.item_id,
                    "status": item.status.value,
                    "result": item.result,
                    "error": item.error,
                }
                for item in job.items
            ]

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a running job.

        Args:
            job_id: The job ID to cancel

        Returns:
            True if cancelled successfully
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False

            if job.status not in (BatchStatus.PENDING, BatchStatus.RUNNING):
                return False

            # Set the per-job stop event to signal cancellation
            job._stop_event.set()
            # Also update status immediately for pending jobs
            if job.status == BatchStatus.PENDING:
                job.status = BatchStatus.CANCELLED

        logger.info(f"Cancelled batch job {job_id}")
        return True

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """
        Remove old completed/failed/cancelled jobs.

        Args:
            max_age_hours: Maximum age of jobs to keep

        Returns:
            Number of jobs removed
        """
        cutoff = time.time() - (max_age_hours * 3600)
        removed = 0

        with self._lock:
            to_remove = []
            for job_id, job in self._jobs.items():
                if job.status in (
                    BatchStatus.COMPLETED,
                    BatchStatus.FAILED,
                    BatchStatus.CANCELLED,
                ):
                    if job.created_at < cutoff:
                        to_remove.append(job_id)

            for job_id in to_remove:
                del self._jobs[job_id]
                removed += 1

        if removed:
            logger.debug(f"Cleaned up {removed} old batch jobs")

        return removed


# Module-level singleton
_batch_processor: Optional[BatchProcessor] = None


def get_batch_processor() -> BatchProcessor:
    """Get the global BatchProcessor instance."""
    global _batch_processor
    if _batch_processor is None:
        from sickchill import settings

        # Use AI rate limits as base
        rate_limit = settings.AI_MAX_CALLS_PER_HOUR // 60  # Convert to per-minute
        rate_limit = max(1, min(rate_limit, 10))  # Clamp to reasonable range

        _batch_processor = BatchProcessor(rate_limit_per_minute=rate_limit)
    return _batch_processor
