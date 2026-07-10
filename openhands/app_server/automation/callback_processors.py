"""Callback processors for execution lifecycle events.

Handles post-execution state updates when conversations complete or fail.
Hooks into the EventCallbackProcessor system to react to terminal states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from openhands.app_server.event_callback.event_callback_models import (
    EventCallback,
    EventCallbackProcessor,
    EventCallbackStatus,
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

if TYPE_CHECKING:
    from fastapi import Request


class AutomationEventCallbackProcessor(EventCallbackProcessor):
    """Event callback processor that updates automation executions.

    Registered on automation-triggered conversations. Listens for
    ConversationStateUpdateEvent with terminal execution_status values
    (FINISHED, ERROR, STUCK) and updates the execution record.

    When state and request are injected via set_request_context(),
    the processor will also automatically pause the sandbox after
    the execution state transition.
    """

    event_kind: ClassVar[EventKind] = 'ConversationStateUpdateEvent'

    _state: object | None = None
    _request: 'Request | None' = None

    def set_request_context(self, state: object, request: 'Request') -> None:
        """Store the request context for sandbox pause on terminal state."""
        self._state = state
        self._request = request

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
            f'[Automation] Execution {record.execution_id} -> {new_state.value} '
            f'(conversation: {conversation_id})',
            extra=build_log_context(
                execution_id=record.execution_id,
                conversation_id=str(conversation_id),
            ),
        )

        # Pause the sandbox if request context is available
        if self._state is not None and self._request is not None:
            await self._pause_sandbox(conversation_id)

        # Disable this callback after terminal event
        callback.status = EventCallbackStatus.COMPLETED

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )

    async def _pause_sandbox(self, conversation_id: UUID) -> None:
        """Pause the sandbox associated with this conversation."""
        try:
            from openhands.app_server.config import (
                get_app_conversation_info_service,
            )
            from openhands.app_server.utils.sandbox_utils import (
                pause_sandbox,
            )

            async with get_app_conversation_info_service(
                self._state, self._request
            ) as info_service:
                info = await info_service.get_app_conversation_info(
                    conversation_id
                )
                if info and info.sandbox_id:
                    await pause_sandbox(
                        info.sandbox_id, self._state, self._request
                    )
        except Exception:
            logger.error(
                '[Automation] Failed to pause sandbox for conversation '
                f'{conversation_id}:',
                exc_info=True,
            )


