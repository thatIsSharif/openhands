"""Callback processors for execution lifecycle events.

Handles post-execution state updates when conversations complete or fail.
Hooks into the EventCallbackProcessor system to react to terminal states.
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
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.user.specifiy_user_context import ADMIN, USER_CONTEXT_ATTR
from openhands.app_server.utils.logger import openhands_logger as logger
from openhands.sdk import Event
from openhands.sdk.conversation import ConversationExecutionStatus
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent

from .correlation import build_log_context
from .execution_models import ExecutionState
from .execution_store import ExecutionStore


class AutomationEventCallbackProcessor(EventCallbackProcessor):
    """Event callback processor that updates automation executions.

    Registered on automation-triggered conversations. Listens for
    ConversationStateUpdateEvent with terminal execution_status values
    (FINISHED, ERROR, STUCK) and updates the execution record.
    Also posts a JIRA comment with token usage for JIRA-triggered
    conversations that complete successfully.
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

        # Post token usage to JIRA for any terminal event on a
        # JIRA-triggered conversation. Posts/updates a single comment
        # per issue (identified by *OpenHands Automation Complete* marker).
        if record.jira_issue_key:
            await self._post_jira_token_usage(
                conversation_id=conversation_id,
                jira_issue_key=record.jira_issue_key,
                execution_id=record.execution_id,
                max_budget=record.max_budget,
            )

        # Disable this callback after terminal event
        callback.status = EventCallbackStatus.COMPLETED

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )

    async def _post_jira_token_usage(
        self,
        conversation_id: UUID,
        jira_issue_key: str,
        execution_id: str,
        max_budget: float | None = None,
    ) -> None:
        """Post token usage and cost metrics as a JIRA comment.

        Retrieves accumulated metrics from the conversation and posts
        a summary comment to the linked JIRA issue.

        Args:
            conversation_id: The conversation ID to fetch metrics for.
            jira_issue_key: The JIRA issue key to post the comment to.
            execution_id: The execution ID for logging context.
            max_budget: Optional max budget for the execution.
        """
        from openhands.app_server.config import (
            get_app_conversation_info_service,
        )
        from openhands.app_server.utils.jira import (
            add_or_update_token_usage_comment,
        )

        try:
            # Build a minimal DI state for services that don't need auth
            state = InjectorState()
            setattr(state, USER_CONTEXT_ATTR, ADMIN)

            async with get_app_conversation_info_service(
                state
            ) as info_service:
                conv_info = await info_service.get_app_conversation_info(
                    conversation_id
                )

            if not conv_info:
                logger.warning(
                    f'[Automation] Cannot post JIRA token usage for '
                    f'{jira_issue_key}: conversation info not found'
                )
                return

            metrics = conv_info.metrics
            if not metrics:
                logger.info(
                    f'[Automation] No metrics available for '
                    f'{jira_issue_key}, skipping token usage comment'
                )
                return

            # Build the comment body with token usage details
            token_usage = metrics.accumulated_token_usage
            lines = [
                '*OpenHands Automation Complete*',
                '',
                f'*Total Cost:* ${metrics.accumulated_cost:.6f}',
                f'*Model:* {metrics.model_name}',
            ]

            if token_usage:
                lines.append('')
                lines.append('*Token Usage:*')
                lines.append(
                    f'- Prompt tokens: {token_usage.prompt_tokens:,}'
                )
                lines.append(
                    f'- Completion tokens: {token_usage.completion_tokens:,}'
                )
                lines.append(
                    f'- Total tokens: '
                    f'{token_usage.prompt_tokens + token_usage.completion_tokens:,}'
                )
                if token_usage.cache_read_tokens:
                    lines.append(
                        f'- Cache read tokens: {token_usage.cache_read_tokens:,}'
                    )
                if token_usage.cache_write_tokens:
                    lines.append(
                        f'- Cache write tokens: {token_usage.cache_write_tokens:,}'
                    )
                if token_usage.reasoning_tokens:
                    lines.append(
                        f'- Reasoning tokens: {token_usage.reasoning_tokens:,}'
                    )

            if max_budget and max_budget > 0:
                pct = metrics.accumulated_cost / max_budget * 100
                lines.append('')
                lines.append(
                    f'*Budget Usage:* ${metrics.accumulated_cost:.4f}'
                    f' / ${max_budget:.4f} ({pct:.1f}%)'
                )

            comment_body = '\n'.join(lines)
            result = add_or_update_token_usage_comment(
                jira_issue_key, comment_body
            )
            logger.info(
                f'[Automation] {"Updated" if result.get("id") else "Posted"} '
                f'token usage comment on {jira_issue_key}',
                extra=build_log_context(
                    execution_id=execution_id,
                    conversation_id=str(conversation_id),
                    jira_issue_key=jira_issue_key,
                ),
            )

        except Exception:
            import traceback

            logger.error(
                f'[Automation] Failed to post token usage to {jira_issue_key}: '
                f'{traceback.format_exc()}'
            )
