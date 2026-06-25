"""Budget enforcement processor for automation conversations.

Monitors conversation stats events and interrupts the conversation
when accumulated cost exceeds the configured budget limit.
"""

from __future__ import annotations

import logging
from typing import ClassVar
from uuid import UUID

from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversation,
)
from openhands.app_server.automation.execution_models import ExecutionState
from openhands.app_server.automation.execution_store import ExecutionStore
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
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.user.specifiy_user_context import ADMIN, USER_CONTEXT_ATTR
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)
from openhands.sdk import ConversationStats, Event
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent

_logger = logging.getLogger(__name__)


class BudgetEnforcementProcessor(EventCallbackProcessor):
    """Event callback processor that enforces budget limits.

    Registered on automation-triggered conversations. Listens for
    ConversationStateUpdateEvent with key='stats', extracts the
    accumulated cost, and interrupts the conversation if the cost
    exceeds the execution record's max_budget.
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

        if event.key != 'stats':
            return None

        # Parse the stats value into ConversationStats
        event_value = event.value
        conversation_stats: ConversationStats | None = None

        if isinstance(event_value, ConversationStats):
            conversation_stats = event_value
        elif isinstance(event_value, dict):
            try:
                conversation_stats = ConversationStats.model_validate(event_value)
            except Exception:
                _logger.exception(
                    '[Budget] Failed to parse ConversationStats from event'
                )
                return None

        if not conversation_stats or not conversation_stats.usage_to_metrics:
            return None

        # Get combined metrics (accumulated cost across all LLM usage)
        combined = conversation_stats.get_combined_metrics()
        accumulated_cost = combined.accumulated_cost

        # Look up the execution record for this conversation
        store = ExecutionStore()
        record = await store.get_execution_by_conversation_id(
            str(conversation_id)
        )
        if not record:
            return None

        max_budget = record.max_budget
        if max_budget is None or max_budget <= 0:
            return None

        # Check if budget is exceeded
        if accumulated_cost <= max_budget:
            return None

        # Budget exceeded — interrupt the conversation on the agent server
        _logger.warning(
            '[Budget] Budget exceeded for conversation %s: '
            '$%.4f > $%.4f (max_budget)',
            conversation_id,
            accumulated_cost,
            max_budget,
        )

        await self._interrupt_conversation(conversation_id, conversation_stats)

        # Update the execution record
        await store.update_state(
            execution_id=record.execution_id,
            state=ExecutionState.FAILED,
            error_message=(
                f'Budget exceeded (${accumulated_cost:.2f} > ${max_budget:.2f})'
            ),
        )

        # Disable this callback after enforcement
        callback.status = EventCallbackStatus.COMPLETED

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )

    async def _interrupt_conversation(
        self,
        conversation_id: UUID,
        conversation_stats: ConversationStats,
    ) -> None:
        """Interrupt a running conversation by calling the agent server.

        Uses deferred imports to avoid circular dependencies.
        """
        from openhands.app_server.config import (
            get_app_conversation_service,
            get_httpx_client,
        )

        state = InjectorState()
        setattr(state, USER_CONTEXT_ATTR, ADMIN)
        async with (
            get_app_conversation_service(state) as app_conversation_service,
            get_httpx_client(state) as httpx_client,
        ):
            app_conversation: AppConversation | None = (
                await app_conversation_service.get_app_conversation(
                    conversation_id
                )
            )
            if not app_conversation:
                _logger.warning(
                    '[Budget] AppConversation not found for %s',
                    conversation_id,
                )
                return

            app_conversation_url = app_conversation.conversation_url
            if not app_conversation_url:
                _logger.warning(
                    '[Budget] No conversation URL for %s',
                    conversation_id,
                )
                return

            app_conversation_url = replace_localhost_hostname_for_docker(
                app_conversation_url
            )

            interrupt_url = f'{app_conversation_url}/interrupt'
            try:
                response = await httpx_client.post(
                    interrupt_url,
                    headers={
                        'X-Session-API-Key': app_conversation.session_api_key,
                    },
                    timeout=10.0,
                )
                response.raise_for_status()
                _logger.info(
                    '[Budget] Interrupted conversation %s (status=%d)',
                    conversation_id,
                    response.status_code,
                )
            except Exception:
                _logger.exception(
                    '[Budget] Failed to interrupt conversation %s',
                    conversation_id,
                )
