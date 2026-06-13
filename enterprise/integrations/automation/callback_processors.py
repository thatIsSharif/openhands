"""Callback processors for the automation platform.

These processors listen for conversation state changes and update
execution records when the agent finishes.
"""

from __future__ import annotations

from uuid import UUID

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import ExecutionState
from .execution_service import ExecutionService
from .execution_store import ExecutionStore


class AutomationCallbackProcessor:
    """Handles post-execution updates for automation platform conversations.

    Unlike the existing JiraV1CallbackProcessor/GithubV1CallbackProcessor which
    post summaries back to the source platform, this processor focuses on:
    1. Updating the execution state in the database
    2. Logging completion/failure for observability
    3. Future: triggering follow-up actions (posting to Jira/GitHub, etc.)
    """

    def __init__(
        self,
        execution_id: str,
        execution_service: ExecutionService | None = None,
        execution_store: ExecutionStore | None = None,
    ) -> None:
        self._execution_id = execution_id
        self._execution_service = execution_service or ExecutionService(
            store=execution_store or ExecutionStore()
        )

    async def on_conversation_finished(
        self,
        conversation_id: str,
        status: str = 'completed',
        error_message: str | None = None,
    ) -> None:
        """Called when the OpenHands conversation finishes.

        Updates the execution state to COMPLETED or FAILED.
        """
        log_ctx = build_log_context(
            execution_id=self._execution_id,
            conversation_id=conversation_id,
        )

        if status == 'completed':
            await self._execution_service.transition_state(
                self._execution_id,
                ExecutionState.COMPLETED,
            )
            logger.info(
                f'[AutomationCallback] Execution {self._execution_id} '
                f'completed successfully',
                extra=log_ctx,
            )
        else:
            await self._execution_service.transition_state(
                self._execution_id,
                ExecutionState.FAILED,
                error_message=error_message or 'Conversation finished with error',
            )
            logger.error(
                f'[AutomationCallback] Execution {self._execution_id} '
                f'failed: {error_message}',
                extra=log_ctx,
            )

    async def on_conversation_error(
        self,
        conversation_id: str,
        error: Exception,
    ) -> None:
        """Called when an error occurs during conversation execution."""
        log_ctx = build_log_context(
            execution_id=self._execution_id,
            conversation_id=conversation_id,
        )
        await self._execution_service.transition_state(
            self._execution_id,
            ExecutionState.FAILED,
            error_message=str(error),
        )
        logger.error(
            f'[AutomationCallback] Execution {self._execution_id} '
            f'error: {error}',
            extra=log_ctx,
        )
