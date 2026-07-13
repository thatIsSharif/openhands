"""Callback processor that auto-exports workspace on conversation completion.

Listens for ``ConversationStateUpdateEvent`` with ``event.key == 'full_state'``
and checks ``execution_status`` in the event payload. When the conversation
reaches a terminal state (``finished``, ``error``, ``stopped``) AND has a
``jira_issue_key`` in its conversation metadata, it triggers an export in
the background.
"""

from __future__ import annotations

import json
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
from openhands.sdk import Event
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent

if TYPE_CHECKING:
    from fastapi import Request

_logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({'finished', 'error', 'stopped'})


class ExportOnCompletionCallbackProcessor(EventCallbackProcessor):
    """Automatically export workspace when a conversation completes."""

    event_kind: ClassVar[EventKind] = 'ConversationStateUpdateEvent'

    _state: object | None = None
    _request: 'Request | None' = None

    def set_request_context(self, state: object, request: 'Request') -> None:
        """Store the request context for sandbox export on terminal state."""
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

        if event.key != 'full_state':
            return None

        try:
            payload = (
                json.loads(event.value)
                if isinstance(event.value, str)
                else event.value
            )
        except (json.JSONDecodeError, TypeError):
            return None

        if not isinstance(payload, dict):
            return None

        exec_status = payload.get('execution_status')
        if exec_status not in TERMINAL_STATUSES:
            return None

        jira_key = payload.get('jira_issue_key')
        if not jira_key:
            return None

        _logger.info(
            'Conversation %s reached terminal status "%s" with jira_key=%s — '
            'triggering workspace export',
            conversation_id,
            exec_status,
            jira_key,
        )

        if self._state is not None and self._request is not None:
            try:
                await self._run_export(conversation_id, jira_key)
            except Exception:
                _logger.exception(
                    'Workspace export failed for conversation %s (jira_key=%s)',
                    conversation_id,
                    jira_key,
                )
        else:
            _logger.warning(
                'ExportOnCompletionCallbackProcessor has no request context — '
                'skipping export for conversation %s',
                conversation_id,
            )

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )

    async def _run_export(self, conversation_id: UUID, jira_key: str) -> None:
        """Actually run the export using dependency injection."""
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

