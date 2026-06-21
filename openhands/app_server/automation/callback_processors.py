"""Callback processors for execution lifecycle events.

Handles post-execution state updates when conversations complete or fail.
Hooks into the EventCallbackProcessor system to react to terminal states.
"""

from __future__ import annotations

import json
import os
import urllib.request
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

from .correlation import build_log_context
from .execution_models import ExecutionState, SourceType
from .execution_store import ExecutionStore


def _send_teams_notification(
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

    payload = json.dumps({
        'execution_id': execution_id,
        'state': state.value,
        'jira_issue_key': jira_issue_key,
        'repository': repository,
        'error_message': error_message,
    }).encode('utf-8')

    try:
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(
                f'[Teams] Notification sent for execution {execution_id} '
                f'(HTTP {resp.status})',
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

        # Optionally send Teams push notification
        if record.source_type == SourceType.TEAMS.value:
            _send_teams_notification(
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
