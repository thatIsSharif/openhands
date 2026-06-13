"""Callback processors for execution lifecycle events.

Handles post-execution state updates when conversations complete or fail.
"""

from __future__ import annotations

from dataclasses import dataclass

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import ExecutionState
from .execution_store import ExecutionStore


@dataclass
class AutomationCallbackProcessor:
    """Processes lifecycle callbacks for automation executions.

    This is called by the event system when OpenHands conversations
    reach terminal states (completed/failed), updating the execution
    record accordingly.
    """

    store: ExecutionStore

    async def on_conversation_completed(
        self,
        execution_id: str,
        conversation_id: str,
    ) -> None:
        """Handle successful conversation completion."""
        await self.store.update_state(
            execution_id=execution_id,
            state=ExecutionState.COMPLETED,
            conversation_id=conversation_id,
        )
        logger.info(
            f'[Automation] Execution {execution_id} completed '
            f'(conversation: {conversation_id})',
            extra=build_log_context(
                execution_id=execution_id,
                conversation_id=conversation_id,
            ),
        )

    async def on_conversation_failed(
        self,
        execution_id: str,
        conversation_id: str,
        error: str,
    ) -> None:
        """Handle conversation failure."""
        await self.store.update_state(
            execution_id=execution_id,
            state=ExecutionState.FAILED,
            error_message=error,
            conversation_id=conversation_id,
        )
        logger.error(
            f'[Automation] Execution {execution_id} failed: {error}',
            extra=build_log_context(
                execution_id=execution_id,
                conversation_id=conversation_id,
            ),
        )
