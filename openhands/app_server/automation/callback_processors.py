"""Callback processors for execution lifecycle events.

Simplified: The polling approach in OpenHandsClient handles all
post-execution logic (commit, push, PR creation, Jira updates).
This processor is retained for backward compatibility and logs
terminal state transitions without performing post-execution actions.
"""

from __future__ import annotations

from typing import ClassVar
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


class AutomationEventCallbackProcessor(EventCallbackProcessor):
    """Event callback processor for automation executions.

    Deprecated in favor of the polling-based post-execution flow
    (see OpenHandsClient._poll_and_complete). Retained for backward
    compatibility. Simply logs terminal state transitions without
    performing post-execution actions (commit, push, PR, Jira, etc.).
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

        # Post-execution actions (commit, push, PR, Jira, tokens)
        # are handled by the background polling task in
        # OpenHandsClient._poll_and_complete(). This callback only
        # logs the transition for observability.

        logger.info(
            f'[Automation] Conversation {conversation_id} reached '
            f'terminal state: {exec_status.value}',
        )

        # Disable this callback after terminal event
        callback.status = EventCallbackStatus.COMPLETED

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )


