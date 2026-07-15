"""Callback processors for execution lifecycle events.

Simplified: The polling approach in OpenHandsClient handles all
post-execution logic (commit, push, PR creation, Jira updates).
This processor is retained for backward compatibility — it logs
terminal state transitions and pauses the sandbox when request
context was injected via set_request_context().
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

if TYPE_CHECKING:
    from fastapi import Request


class AutomationEventCallbackProcessor(EventCallbackProcessor):
    """Event callback processor for automation executions.

    Post-execution actions (commit, push, PR, Jira, tokens) are
    handled by the background polling task in
    OpenHandsClient._poll_and_complete(). This processor logs
    terminal state transitions for observability and pauses the
    sandbox when request context has been injected.
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

        # Post-execution actions (commit, push, PR, Jira, tokens)
        # are handled by the background polling task in
        # OpenHandsClient._poll_and_complete(). This callback only
        # logs the transition for observability.

        logger.info(
            f'[Automation] Conversation {conversation_id} reached '
            f'terminal state: {exec_status.value}',
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


