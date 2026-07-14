"""Callback processor that auto-exports workspace on conversation completion.

Listens for ``ConversationStateUpdateEvent`` with ``event.key == 'execution_status'``.
When the conversation reaches a terminal state and has a ``jira_issue_key`` in
its conversation metadata, it triggers an export via ``WorkspaceExportService``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar
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
from openhands.sdk import ConversationExecutionStatus, Event
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent

if TYPE_CHECKING:
    from fastapi import Request

_logger = logging.getLogger(__name__)


class ExportOnCompletionCallbackProcessor(EventCallbackProcessor):
    """Automatically export workspace when a conversation with a Jira
    issue key reaches terminal state."""

    event_kind: ClassVar[EventKind] = 'ConversationStateUpdateEvent'

    _state: object | None = None
    _request: 'Request | None' = None

    def set_request_context(self, state: object, request: 'Request') -> None:
        """Store the request context for DI on terminal state."""
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

        # Need request context to call DI-backed services
        if self._state is None or self._request is None:
            _logger.warning(
                'ExportOnCompletionCallbackProcessor has no request context — '
                'skipping export for conversation %s',
                conversation_id,
            )
            return None

        # Look up jira_issue_key from the database
        from openhands.app_server.config import (
            get_app_conversation_info_service,
        )

        jira_key: str | None = None
        try:
            async with get_app_conversation_info_service(
                self._state, self._request
            ) as info_service:
                info = await info_service.get_app_conversation_info(conversation_id)
                if info:
                    jira_key = info.jira_issue_key
        except Exception:
            _logger.exception(
                'Failed to fetch conversation info for export check (%s)',
                conversation_id,
            )

        if not jira_key:
            _logger.debug(
                'No jira_issue_key for conversation %s — skipping export',
                conversation_id,
            )
            return None

        _logger.info(
            'Conversation %s reached terminal status "%s" with jira_key=%s — '
            'triggering workspace export',
            conversation_id,
            exec_status.value,
            jira_key,
        )

        try:
            await self._run_export(conversation_id, jira_key)
        except Exception:
            _logger.exception(
                'Workspace export failed for conversation %s (jira_key=%s)',
                conversation_id,
                jira_key,
            )

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )

    async def _run_export(self, conversation_id: UUID, jira_key: str) -> None:
        """Run the export using dependency injection."""
        from openhands.app_server.config import (
            get_app_conversation_info_service,
            get_app_conversation_service,
            get_sandbox_service,
            get_workspace_export_service,
        )

        async with get_app_conversation_service(
            self._state, self._request
        ) as app_conversation_service:
            async with get_app_conversation_info_service(
                self._state, self._request
            ) as app_conversation_info_service:
                async with get_sandbox_service(
                    self._state, self._request
                ) as sandbox_service:
                    async with get_workspace_export_service(
                        self._state, self._request
                    ) as export_service:
                        result = await export_service.export_conversation(
                            conversation_id=conversation_id,
                            jira_key=jira_key,
                            app_conversation_service=app_conversation_service,
                            app_conversation_info_service=app_conversation_info_service,
                            docker_sandbox_service=sandbox_service,
                        )
                        if result.success:
                            _logger.info(
                                'Workspace export succeeded for %s (tag: %s)',
                                jira_key,
                                result.snapshot_tag,
                            )
                        else:
                            _logger.warning(
                                'Workspace export failed for %s: %s',
                                jira_key,
                                result.error_message,
                            )

