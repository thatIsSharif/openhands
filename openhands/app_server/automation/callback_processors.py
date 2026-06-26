"""Callback processors for execution lifecycle events.

Handles post-execution state updates when conversations complete or fail.
Hooks into the EventCallbackProcessor system to react to terminal states.
"""

from __future__ import annotations

import logging
from typing import ClassVar
from uuid import UUID

from openhands.app_server.event_callback.event_callback_models import (
    EventCallback,
    EventCallbackProcessor,
    EventKind,
)
from openhands.app_server.event_callback.event_callback_result_models import (
    EventCallbackResult,
    EventCallbackResultStatus,
)
from openhands.app_server.utils.logger import openhands_logger as logger
from openhands.sdk import Event
from openhands.sdk.conversation import ConversationExecutionStatus
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent

from .correlation import build_log_context
from .execution_models import ExecutionState
from .execution_store import ExecutionStore

_logger = logging.getLogger(__name__)


class AutomationEventCallbackProcessor(EventCallbackProcessor):
    """Event callback processor that updates automation executions.

    Registered on automation-triggered conversations. Listens for
    ConversationStateUpdateEvent with terminal execution_status values
    (FINISHED, ERROR, STUCK) and updates the execution record.
    """

    event_kind: ClassVar[EventKind] = 'ConversationStateUpdateEvent'

    async def __call__(
        self,
        conversation_id: UUID,
        callback: EventCallback,
        event: Event,
    ) -> EventCallbackResult | None:
        if not isinstance(event, ConversationStateUpdateEvent):
            return None

        if event.key != 'execution_status':
            return None

        try:
            exec_status = ConversationExecutionStatus(event.value)
        except (ValueError, TypeError):
            return None

        if not exec_status.is_terminal():
            return None

        # Look up the execution record via the conversation_id
        store = ExecutionStore()
        record = await store.get_execution_by_conversation_id(
            str(conversation_id)
        )
        if not record:
            logger.info(
                '[Automation] No execution record found for '
                f'conversation {conversation_id} (may not be automation)'
            )
            return None

        # Determine execution state from conversation status
        if exec_status == ConversationExecutionStatus.FINISHED:
            new_state = ExecutionState.COMPLETED
        else:
            new_state = ExecutionState.FAILED

        await store.update_state(
            execution_id=record.execution_id,
            state=new_state,
            conversation_id=str(conversation_id),
        )

        logger.info(
            f'[Automation] Execution {record.execution_id} → {new_state.value} '
            f'(conversation: {conversation_id})',
            extra=build_log_context(
                execution_id=record.execution_id,
                conversation_id=str(conversation_id),
            ),
        )

        # Pause the sandbox so it can be resumed on the next Jira comment.
        # We intentionally do NOT set callback.status to COMPLETED here —
        # the callback stays ACTIVE so it fires again when a follow-up
        # Jira comment triggers a new run (execution_status transitions
        # FINISHED → RUNNING → FINISHED).
        await self._pause_sandbox(conversation_id)

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )

    async def _pause_sandbox(self, conversation_id: UUID) -> None:
        """Pause the sandbox for this conversation.

        Uses deferred imports to avoid circular dependencies, following the
        same pattern as BudgetEnforcementProcessor._interrupt_conversation.
        """
        from openhands.app_server.config import (
            get_app_conversation_service,
            get_sandbox_service,
        )

        from openhands.app_server.services.injector import InjectorState
        from openhands.app_server.user.specifiy_user_context import (
            ADMIN,
            USER_CONTEXT_ATTR,
        )

        state = InjectorState()
        setattr(state, USER_CONTEXT_ATTR, ADMIN)
        async with (
            get_app_conversation_service(state) as app_conversation_service,
            get_sandbox_service(state) as sandbox_service,
        ):
            app_conversation = await app_conversation_service.get_app_conversation(
                conversation_id
            )
            if not app_conversation:
                _logger.warning(
                    '[Automation] AppConversation not found for %s, '
                    'cannot pause sandbox',
                    conversation_id,
                )
                return

            sandbox_id = app_conversation.sandbox_id
            if not sandbox_id:
                _logger.warning(
                    '[Automation] No sandbox_id for conversation %s, '
                    'cannot pause sandbox',
                    conversation_id,
                )
                return

            try:
                await sandbox_service.pause_sandbox(sandbox_id)
                _logger.info(
                    '[Automation] Paused sandbox %s for conversation %s',
                    sandbox_id,
                    conversation_id,
                )
            except Exception:
                _logger.exception(
                    '[Automation] Failed to pause sandbox %s for conversation %s',
                    sandbox_id,
                    conversation_id,
                )
