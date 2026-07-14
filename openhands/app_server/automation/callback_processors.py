"""Callback processors for execution lifecycle events.

Handles post-execution state transitions: when a conversation reaches a
terminal state the processor archives the conversation + workspace to S3
and destroys the sandbox instead of leaving it paused.
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
from .sandbox_archive_service import SandboxArchiveService

if TYPE_CHECKING:
    from fastapi import Request


class AutomationEventCallbackProcessor(EventCallbackProcessor):
    """Event callback processor that archives automation executions.

    Registered on automation-triggered conversations. Listens for
    ConversationStateUpdateEvent with terminal execution_status values
    (FINISHED, ERROR, STUCK), marks the execution COMPLETED/FAILED,
    archives conversation state to S3, and destroys the sandbox.

    When state and request are injected via set_request_context(),
    the processor will also automatically archive and cleanup after
    the execution state transition.
    """

    event_kind: ClassVar[EventKind] = 'ConversationStateUpdateEvent'

    _state: object | None = None
    _request: 'Request | None' = None

    def set_request_context(self, state: object, request: 'Request') -> None:
        """Store the request context for archive + cleanup on terminal state."""
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

        # Archive to S3 and destroy sandbox
        if self._state is not None and self._request is not None:
            await self._archive_and_delete_sandbox(
                conversation_id, record.execution_id,
            )

        callback.status = EventCallbackStatus.COMPLETED

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )

    async def _archive_and_delete_sandbox(
        self, conversation_id: UUID, execution_id: str,
    ) -> None:
        """Archive conversation state to S3 then destroy the sandbox."""
        try:
            from openhands.app_server.config import (
                get_app_conversation_info_service,
                get_httpx_client,
                get_sandbox_service,
            )
            from openhands.app_server.file_store.s3 import S3FileStore
            from openhands.app_server.sandbox.sandbox_models import (
                AGENT_SERVER,
            )
            from openhands.app_server.utils.docker_utils import (
                replace_localhost_hostname_for_docker,
            )

            async with (
                get_app_conversation_info_service(
                    self._state, self._request
                ) as info_service,
                get_sandbox_service(
                    self._state, self._request
                ) as sandbox_service,
                get_httpx_client(
                    self._state, self._request
                ) as httpx_client,
            ):
                info = await info_service.get_app_conversation_info(
                    conversation_id
                )
                if not info or not info.sandbox_id:
                    return

                sandbox = await sandbox_service.get_sandbox(info.sandbox_id)
                if not sandbox:
                    return

                # Resolve agent server URL
                agent_url = None
                for eu in sandbox.exposed_urls or []:
                    if eu.name == AGENT_SERVER:
                        agent_url = eu.url
                        break
                if not agent_url:
                    return
                agent_url = replace_localhost_hostname_for_docker(agent_url)

                # Build mapping key
                store = ExecutionStore()
                record = await store.get_execution(execution_id)
                if not record:
                    return
                mapping_key = SandboxArchiveService.build_mapping_key(
                    jira_issue_key=record.jira_issue_key,
                    owner=info.selected_repository.split('/')[0]
                    if info.selected_repository and '/' in info.selected_repository
                    else None,
                    repo=info.selected_repository.split('/')[1]
                    if info.selected_repository and '/' in info.selected_repository
                    else info.selected_repository,
                    pr_number=info.pr_number[0] if info.pr_number else None,
                )

                s3_store = S3FileStore()

                archive_svc = SandboxArchiveService(
                    s3_store=s3_store,
                    httpx_client=httpx_client,
                )
                s3_key = await archive_svc.archive_and_cleanup(
                    agent_server_url=agent_url,
                    session_api_key=sandbox.session_api_key,
                    sandbox_id=info.sandbox_id,
                    conversation_id=str(conversation_id),
                    execution_id=execution_id,
                    mapping_key=mapping_key,
                    sandbox_service=sandbox_service,
                )
                if s3_key:
                    await store.set_archive_location(execution_id, s3_key)
                    logger.info(
                        '[Automation] Archived execution %s to %s',
                        execution_id, s3_key,
                    )
        except Exception:
            logger.error(
                '[Automation] Failed to archive sandbox for conversation '
                f'{conversation_id}:', exc_info=True,
            )


