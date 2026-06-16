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

# Force debug logging for MLflow tracker
logger.setLevel(logging.DEBUG)

_DEBUG_PREFIX = '[MLFLOW_TRACKER]'


def _debug(msg: str, *args, **kwargs):
    """Emit a debug log with a consistent prefix."""
    logger.debug(f'{_DEBUG_PREFIX} {msg}', *args, **kwargs)


def _info(msg: str, *args, **kwargs):
    """Emit an info log with a consistent prefix."""
    logger.info(f'{_DEBUG_PREFIX} {msg}', *args, **kwargs)


def _warn(msg: str, *args, **kwargs):
    """Emit a warning log with a consistent prefix."""
    logger.warning(f'{_DEBUG_PREFIX} {msg}', *args, **kwargs)


def _error(msg: str, *args, **kwargs):
    """Emit an error log with a consistent prefix."""
    logger.error(f'{_DEBUG_PREFIX} {msg}', *args, **kwargs)


def is_mlflow_enabled() -> bool:
    """Check whether MLflow tracking is configured and should be enabled.

    MLflow is enabled when:
    1. ``MLFLOW_TRACKING_URI`` is set to a non-empty value, OR
    2. ``MLFLOW_ENABLED`` is set to 'true' or '1'

    In case 2, the tracker will use the value of ``MLFLOW_TRACKING_URI``
    (defaulting to ``http://localhost:5000``) and the experiment name from
    ``MLFLOW_EXPERIMENT_NAME`` (defaulting to ``openhands-conversations``).
    """
    tracking_uri = os.environ.get('MLFLOW_TRACKING_URI', '').strip()
    enabled_override = os.environ.get('MLFLOW_ENABLED', '').strip().lower() in (
        'true',
        '1',
    )
    result = bool(tracking_uri) or enabled_override
    _debug(
        f'is_mlflow_enabled() -> MLFLOW_TRACKING_URI="{tracking_uri}" '
        f'MLFLOW_ENABLED={enabled_override} → enabled={result}'
    )
    return result


def get_tracking_uri() -> str:
    """Return the MLflow tracking URI from environment or default."""
    val = os.environ.get('MLFLOW_TRACKING_URI', 'http://localhost:5000')
    _debug(f'get_tracking_uri() -> "{val}"')
    return val


def get_experiment_name() -> str:
    """Return the MLflow experiment name from environment or default."""
    val = os.environ.get('MLFLOW_EXPERIMENT_NAME', 'openhands-conversations')
    _debug(f'get_experiment_name() -> "{val}"')
    return val


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
        _debug('initialize() called')
        if not is_mlflow_enabled():
            _info('MLflow tracking not configured (MLFLOW_TRACKING_URI is empty)')
            self._enabled = False
            return

        try:
            import mlflow

            _info(
                f'Initializing MLflow: uri="{self._tracking_uri}" '
                f'experiment="{self._experiment_name}"'
            )
            mlflow.set_tracking_uri(self._tracking_uri)
            mlflow.set_experiment(self._experiment_name)
            self._enabled = True
            _info(
                'MLflow tracking enabled: uri=%s experiment=%s',
                self._tracking_uri,
                self._experiment_name,
            )
        except ImportError:
            _warn('MLflow package not installed. Install with: pip install mlflow')
            self._enabled = False
        except Exception as exc:
            _warn(f'MLflow initialization FAILED: {exc}. Tracking disabled.')
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
        conv_id = str(conversation_id)
        _debug(f'start_conversation({conv_id}) called, enabled={self._enabled}')

        if not self._enabled:
            _debug(f'start_conversation({conv_id}) SKIPPED: MLflow not enabled')
            return False

        meta = metadata or {}
        _debug(f'start_conversation({conv_id}) metadata keys: {list(meta.keys())}')

        try:
            import mlflow

            _debug(f'start_conversation({conv_id}): calling mlflow.start_run()')
            run = mlflow.start_run(run_name=conv_id)
            run_id = run.info.run_id
            _info(
                f'start_conversation({conv_id}): mlflow.start_run() SUCCEEDED, '
                f'run_id={run_id}'
            )

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

            _debug(
                f'start_conversation({conv_id}): logging params: {params}'
            )
            mlflow.log_params(params)
            _debug(
                f'start_conversation({conv_id}): mlflow.log_params() SUCCEEDED'
            )

            state = ConversationRunState(
                conversation_id=conv_id,
                run_id=run_id,
                start_time=time.time(),
            )

            with self._lock:
                self._runs[conv_id] = state
                _debug(
                    f'start_conversation({conv_id}): registered in _runs, '
                    f'active runs now={len(self._runs)}'
                )

            return True

        except Exception as exc:
            _warn(f'start_conversation({conv_id}) FAILED: {exc}')
            import traceback
            _debug(
                f'start_conversation({conv_id}) stack:\n'
                f'{traceback.format_exc()}'
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
        conv_id = str(conversation_id)
        _debug(
            f'log_metrics({conv_id}) called, enabled={self._enabled}, '
            f'metrics_keys={list(metrics.keys())}'
        )

        if not self._enabled:
            _debug(f'log_metrics({conv_id}) SKIPPED: MLflow not enabled')
            return False

        with self._lock:
            state = self._runs.get(conv_id)
            if state is None:
                _debug(
                    f'log_metrics({conv_id}) SKIPPED: no active run found '
                    f'(active runs: {list(self._runs.keys())})'
                )
                return False
            state.turn_count += 1
            effective_step = step if step is not None else state.turn_count
            _debug(
                f'log_metrics({conv_id}): found run, turn_count={state.turn_count}, '
                f'step={effective_step}'
            )

        # Filter out None values and convert to float
        filtered = {}
        for key, value in metrics.items():
            if value is not None:
                filtered[key] = float(value)

        if not filtered:
            _debug(f'log_metrics({conv_id}) SKIPPED: all metric values are None')
            return False

        _debug(f'log_metrics({conv_id}): filtered metrics to log: {filtered}')

        try:
            import mlflow

            _debug(f'log_metrics({conv_id}): calling mlflow.log_metrics()')
            mlflow.log_metrics(filtered, step=effective_step)
            _debug(f'log_metrics({conv_id}): mlflow.log_metrics() SUCCEEDED')

            # Cache the last metrics for final summary
            with self._lock:
                state.last_metrics.update(filtered)

            return True

        except Exception as exc:
            _warn(f'log_metrics({conv_id}) FAILED: {exc}')
            import traceback
            _debug(
                f'log_metrics({conv_id}) stack:\n{traceback.format_exc()}'
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
        conv_id = str(conversation_id)
        _debug(
            f'end_conversation({conv_id}) called, enabled={self._enabled}, '
            f'status={status}'
        )

        if not self._enabled:
            _debug(f'end_conversation({conv_id}) SKIPPED: MLflow not enabled')
            return False

        with self._lock:
            state = self._runs.pop(conv_id, None)
            if state is None:
                _debug(
                    f'end_conversation({conv_id}) SKIPPED: no active run '
                    f'(active runs: {list(self._runs.keys())})'
                )
                return False
            _debug(
                f'end_conversation({conv_id}): found run, '
                f'run_id={state.run_id}, elapsed={time.time()-state.start_time:.2f}s'
            )

        try:
            import mlflow

            duration = time.time() - state.start_time

            params: dict[str, str] = {
                'end_time': datetime.now(timezone.utc).isoformat(),
                'status': status,
                'duration_seconds': f'{duration:.2f}',
                'total_turns': str(state.turn_count),
            }
            _debug(f'end_conversation({conv_id}): logging end params: {params}')
            mlflow.log_params(params)
            _debug(
                f'end_conversation({conv_id}): mlflow.log_params() SUCCEEDED'
            )

            if error:
                _debug(f'end_conversation({conv_id}): logging error artifact')
                mlflow.log_text(error, 'error.txt')
                _debug(
                    f'end_conversation({conv_id}): error artifact logged'
                )

            if summary:
                _debug(
                    f'end_conversation({conv_id}): logging summary artifact'
                )
                mlflow.log_text(summary, 'conversation_summary.txt')

            _debug(f'end_conversation({conv_id}): calling mlflow.end_run()')
            mlflow.end_run()
            _info(
                f'end_conversation({conv_id}): SUCCESS, '
                f'duration={duration:.2f}s, turns={state.turn_count}'
            )
            return True

        except Exception as exc:
            _warn(f'end_conversation({conv_id}) FAILED: {exc}')
            import traceback
            _debug(
                f'end_conversation({conv_id}) stack:\n'
                f'{traceback.format_exc()}'
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
                _debug('Creating MLflowTracker singleton (first call)')
                _debug(
                    'Environment vars: '
                    f'MLFLOW_TRACKING_URI="{os.environ.get("MLFLOW_TRACKING_URI", "")}", '
                    f'MLFLOW_EXPERIMENT_NAME="{os.environ.get("MLFLOW_EXPERIMENT_NAME", "")}"'
                )
                tracker = MLflowTracker()
                tracker.initialize()
                _info(
                    f'MLflowTracker initialized: enabled={tracker.enabled}'
                )
                _tracker = tracker
    else:
        _debug(f'get_mlflow_tracker() -> existing tracker, enabled={_tracker.enabled}')
    return _tracker
