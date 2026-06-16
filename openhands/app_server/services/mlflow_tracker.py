"""MLflow tracking service for OpenHands conversation observability.

Captures token usage, cost, latency, and conversation metadata as MLflow runs.
Designed to be safe when MLflow is unavailable — failures are logged and isolated.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def is_mlflow_enabled() -> bool:
    """Check whether MLflow tracking is configured and should be enabled."""
    return bool(os.environ.get('MLFLOW_TRACKING_URI', '').strip())


def get_tracking_uri() -> str:
    """Return the MLflow tracking URI from environment or default."""
    return os.environ.get('MLFLOW_TRACKING_URI', 'http://localhost:5000')


def get_experiment_name() -> str:
    """Return the MLflow experiment name from environment or default."""
    return os.environ.get('MLFLOW_EXPERIMENT_NAME', 'openhands-conversations')


@dataclass
class ConversationRunState:
    """Per-conversation MLflow run tracking state."""

    conversation_id: str
    run_id: str | None = None
    start_time: float = 0.0
    turn_count: int = 0
    last_metrics: dict[str, float] = field(default_factory=dict)
    status: str = 'running'


class MLflowTracker:
    """Thread-safe MLflow tracker for OpenHands conversations.

    Supports multiple concurrent conversations by maintaining a per-conversation
    run registry. Each conversation gets its own MLflow run identified by the
    conversation UUID.

    Usage:
        tracker = MLflowTracker()
        if tracker.enabled:
            tracker.start_conversation(conversation_id, metadata={...})
            tracker.log_metrics(conversation_id, {...})
            tracker.end_conversation(conversation_id, status='completed')
    """

    def __init__(
        self,
        tracking_uri: str | None = None,
        experiment_name: str | None = None,
    ):
        self._tracking_uri = tracking_uri or get_tracking_uri()
        self._experiment_name = experiment_name or get_experiment_name()
        self._enabled = False
        self._lock = threading.Lock()
        self._runs: dict[str, ConversationRunState] = {}

    @property
    def enabled(self) -> bool:
        """Whether MLflow tracking is active and ready."""
        return self._enabled

    def initialize(self) -> None:
        """Initialize the MLflow connection.

        Safe to call multiple times. If MLflow is unavailable or not configured,
        logs a warning and disables tracking.
        """
        if not is_mlflow_enabled():
            logger.debug(
                'MLflow tracking not configured (MLFLOW_TRACKING_URI is empty)'
            )
            self._enabled = False
            return

        try:
            import mlflow

            mlflow.set_tracking_uri(self._tracking_uri)
            mlflow.set_experiment(self._experiment_name)
            self._enabled = True
            logger.info(
                'MLflow tracking enabled: uri=%s experiment=%s',
                self._tracking_uri,
                self._experiment_name,
            )
        except ImportError:
            logger.warning(
                'MLflow package not installed. Install with: pip install mlflow'
            )
            self._enabled = False
        except Exception as exc:
            logger.warning(
                'MLflow initialization failed: %s. Tracking disabled.',
                exc,
            )
            self._enabled = False

    def start_conversation(
        self,
        conversation_id: UUID | str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Start an MLflow run for a conversation.

        Args:
            conversation_id: The conversation UUID.
            metadata: Optional dict with keys like 'repository', 'branch',
                      'trigger', 'llm_model', 'title', etc.

        Returns:
            True if the run was started successfully, False otherwise.
        """
        if not self._enabled:
            return False

        conv_id = str(conversation_id)
        meta = metadata or {}

        try:
            import mlflow

            run = mlflow.start_run(run_name=conv_id)
            run_id = run.info.run_id

            params: dict[str, str] = {
                'conversation_id': conv_id,
                'start_time': datetime.now(timezone.utc).isoformat(),
                'repository': meta.get('repository') or 'none',
                'branch': meta.get('branch') or 'main',
                'trigger': meta.get('trigger') or 'unknown',
                'llm_model': meta.get('llm_model') or 'unknown',
            }
            if meta.get('title'):
                params['title'] = str(meta['title'])
            if meta.get('sandbox_id'):
                params['sandbox_id'] = str(meta['sandbox_id'])

            mlflow.log_params(params)

            state = ConversationRunState(
                conversation_id=conv_id,
                run_id=run_id,
                start_time=time.time(),
            )

            with self._lock:
                self._runs[conv_id] = state

            logger.debug(
                'MLflow run started: conversation=%s run_id=%s',
                conv_id,
                run_id,
            )
            return True

        except Exception as exc:
            logger.warning(
                'MLflow start_conversation failed for %s: %s',
                conv_id,
                exc,
            )
            return False

    def log_metrics(
        self,
        conversation_id: UUID | str,
        metrics: dict[str, float | int | None],
        step: int | None = None,
    ) -> bool:
        """Log metrics for a conversation.

        Args:
            conversation_id: The conversation UUID.
            metrics: Dict of metric names to values (None values are skipped).
            step: Optional step/iteration number.

        Returns:
            True if metrics were logged, False otherwise.
        """
        if not self._enabled:
            return False

        conv_id = str(conversation_id)

        with self._lock:
            state = self._runs.get(conv_id)
            if state is None:
                return False
            state.turn_count += 1
            effective_step = step if step is not None else state.turn_count

        # Filter out None values and convert to float
        filtered = {}
        for key, value in metrics.items():
            if value is not None:
                filtered[key] = float(value)

        if not filtered:
            return False

        try:
            import mlflow

            mlflow.log_metrics(filtered, step=effective_step)

            # Cache the last metrics for final summary
            with self._lock:
                state.last_metrics.update(filtered)

            return True

        except Exception as exc:
            logger.warning(
                'MLflow log_metrics failed for %s: %s',
                conv_id,
                exc,
            )
            return False

    def end_conversation(
        self,
        conversation_id: UUID | str,
        status: str = 'completed',
        error: str | None = None,
        summary: str | None = None,
    ) -> bool:
        """End an MLflow run for a conversation.

        Args:
            conversation_id: The conversation UUID.
            status: Final status ('completed', 'error', 'stopped').
            error: Optional error description for failed conversations.
            summary: Optional conversation summary text.

        Returns:
            True if the run was ended successfully, False otherwise.
        """
        if not self._enabled:
            return False

        conv_id = str(conversation_id)

        with self._lock:
            state = self._runs.pop(conv_id, None)
            if state is None:
                return False

        try:
            import mlflow

            duration = time.time() - state.start_time

            params: dict[str, str] = {
                'end_time': datetime.now(timezone.utc).isoformat(),
                'status': status,
                'duration_seconds': f'{duration:.2f}',
                'total_turns': str(state.turn_count),
            }
            mlflow.log_params(params)

            if error:
                mlflow.log_text(error, 'error.txt')

            if summary:
                mlflow.log_text(summary, 'conversation_summary.txt')

            mlflow.end_run()

            logger.debug(
                'MLflow run ended: conversation=%s status=%s duration=%.2fs',
                conv_id,
                status,
                duration,
            )
            return True

        except Exception as exc:
            logger.warning(
                'MLflow end_conversation failed for %s: %s',
                conv_id,
                exc,
            )
            return False

    def get_active_run_count(self) -> int:
        """Return the number of currently active MLflow runs."""
        with self._lock:
            return len(self._runs)


# Module-level singleton
_tracker: MLflowTracker | None = None
_tracker_lock = threading.Lock()


def get_mlflow_tracker() -> MLflowTracker:
    """Get or create the global MLflowTracker singleton.

    The tracker is initialized on first access based on environment configuration.
    """
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                tracker = MLflowTracker()
                tracker.initialize()
                _tracker = tracker
    return _tracker
