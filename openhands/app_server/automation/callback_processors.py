"""Callback processors for execution lifecycle events.

Handles post-execution state updates when conversations complete or fail.
Hooks into the EventCallbackProcessor system to react to terminal states.
"""

from __future__ import annotations

import os
from typing import ClassVar
from uuid import UUID

import httpx

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
from .execution_models import ExecutionState, SourceType
from .execution_store import ExecutionStore


async def _send_teams_notification(
    execution_id: str,
    state: ExecutionState,
    jira_issue_key: str | None = None,
    repository: str | None = None,
    error_message: str | None = None,
) -> None:
    """Send a completion notification to the Teams Power Automate webhook.

    Reads ``TEAMS_NOTIFICATION_WEBHOOK_URL`` from the environment.
    This is a fire-and-forget call — failures are logged but not raised.
    """
    webhook_url = os.environ.get('TEAMS_NOTIFICATION_WEBHOOK_URL', '')
    if not webhook_url:
        return

    payload = {
        'execution_id': execution_id,
        'state': state.value,
        'jira_issue_key': jira_issue_key,
        'repository': repository,
        'error_message': error_message,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
            )
            logger.info(
                f'[Teams] Notification sent for execution {execution_id} '
                f'(HTTP {resp.status_code})',
            )
    except Exception as e:
        logger.warning(
            f'[Teams] Failed to send notification for execution '
            f'{execution_id}: {e}',
        )


class AutomationEventCallbackProcessor(EventCallbackProcessor):
    """Event callback processor that updates automation executions.

    Registered on automation-triggered conversations. Listens for
    ConversationStateUpdateEvent with terminal execution_status values
    (FINISHED, ERROR, STUCK) and updates the execution record.
    Optionally sends a push notification to the Teams Power Automate
    webhook if ``TEAMS_NOTIFICATION_WEBHOOK_URL`` is set.
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

        # Optionally send Teams push notification.
        # Fires for executions with a jira_issue_key OR started from Teams,
        # when TEAMS_NOTIFICATION_WEBHOOK_URL is configured.
        if (
            record.jira_issue_key
            or record.source_type == SourceType.TEAMS.value
        ):
            await _send_teams_notification(
                execution_id=record.execution_id,
                state=new_state,
                jira_issue_key=record.jira_issue_key,
                repository=record.repository,
                error_message=(
                    record.error_message
                    if new_state == ExecutionState.FAILED
                    else None
                ),
            )

        # Disable this callback after terminal event
        callback.status = EventCallbackStatus.COMPLETED

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )
