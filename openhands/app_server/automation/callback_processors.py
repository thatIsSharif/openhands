"""Callback processors for execution lifecycle events.

Handles post-execution state updates when conversations complete or fail.
Hooks into the EventCallbackProcessor system to react to terminal states
and auto-reject pending actions when the conversation enters the
WAITING_FOR_CONFIRMATION state (no user available to approve in automation).
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

from .correlation import build_log_context
from .execution_models import ExecutionState
from .execution_store import ExecutionStore


class AutomationEventCallbackProcessor(EventCallbackProcessor):
    """Event callback processor for automation conversations.

    --- Terminal states ---
    Listens for ConversationStateUpdateEvent with terminal execution_status
    (FINISHED, ERROR, STUCK) and updates the execution record.

    --- WAITING_FOR_CONFIRMATION (auto-reject) ---
    When the conversation enters WAITING_FOR_CONFIRMATION, the security
    analyzer has flagged a HIGH-risk action and the ConfirmRisky policy
    triggered. In automation there is no user to approve, so the processor
    automatically rejects the pending action by POST-ing to the agent
    server's ``/respond_to_confirmation`` endpoint. The agent receives a
    ``UserRejectObservation`` and skips the action.

    ``agent_server_url`` and ``session_api_key`` are populated by the
    conversation-start flow before the EventCallback is persisted.
    """

    event_kind: ClassVar[EventKind] = 'ConversationStateUpdateEvent'

    agent_server_url: str | None = None
    """Set by the conversation-start flow before the callback is saved."""

    session_api_key: str | None = None
    """Set by the conversation-start flow before the callback is saved."""

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

        # --- WAITING_FOR_CONFIRMATION → auto-reject (no user in automation) ---
        if exec_status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION:
            await self._auto_reject(
                conversation_id=conversation_id,
                callback=callback,
            )
            return None

        # --- Terminal states (existing logic) --------------------------------
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

        # Disable this callback after terminal event
        callback.status = EventCallbackStatus.COMPLETED

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )

    async def _auto_reject(
        self,
        conversation_id: UUID,
        callback: EventCallback,
    ) -> None:
        """Auto-reject pending actions when conversation is waiting for confirmation.

        1. POSTs a rejection to the agent server's ``respond_to_confirmation``
           endpoint so the agent receives a ``UserRejectObservation`` (the
           dangerous action is never executed).
        2. POSTs to ``/{conversation_id}/run`` to restart the agent loop so
           it continues to its next step instead of staying idle.

        Both calls use ``X-Session-API-Key`` for authentication and the
        ``agent_server_url`` obtained from the sandbox at conversation-start
        time.
        """
        if not self.agent_server_url or not self.session_api_key:
            logger.warning(
                '[Automation] Cannot auto-reject conversation %s: '
                'agent_server_url or session_api_key not set',
                conversation_id,
            )
            return

        base = self.agent_server_url.rstrip('/')
        headers = {'X-Session-API-Key': self.session_api_key}

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                # Step 1: Reject the pending actions.
                reject_resp = await client.post(
                    f'{base}/api/conversations/'
                    f'{conversation_id}/events/respond_to_confirmation',
                    json={
                        'accept': False,
                        'reason': (
                            'Auto-rejected by automation security policy — '
                            'no user available to confirm.'
                        ),
                    },
                    headers=headers,
                )
                reject_resp.raise_for_status()

                # Step 2: Restart the agent loop so it continues to the
                # next step instead of staying idle.
                run_resp = await client.post(
                    f'{base}/api/conversations/{conversation_id}/run',
                    headers=headers,
                )
                # 409 is expected if the conversation is already running,
                # which is fine — it means the agent is already moving.
                if run_resp.status_code not in (200, 409):
                    run_resp.raise_for_status()

            logger.info(
                '[Automation] Auto-rejected pending actions and restarted '
                'conversation %s',
                conversation_id,
                extra=build_log_context(
                    conversation_id=str(conversation_id),
                ),
            )

        except Exception as e:
            logger.warning(
                '[Automation] Failed to auto-reject for '
                'conversation %s: %s',
                conversation_id,
                e,
                extra=build_log_context(
                    conversation_id=str(conversation_id),
                ),
            )


