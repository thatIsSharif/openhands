"""Command enforcement processor for automation conversations.

Layer 2 security: monitors agent terminal commands via ActionEvent
callbacks and interrupts the conversation if a dangerous command is
detected. When a dangerous command is identified, the conversation is
stopped and a security alert comment is posted back to the source
(GitHub PR or Jira issue).

Works alongside the ``block_dangerous.sh`` PreToolUse hook (injected via
``hook_files.py``) which blocks commands at the runtime level *before*
execution. This processor handles the post-hoc notification layer:
posting a comment to GitHub/Jira and stopping the conversation.
"""

from __future__ import annotations

import logging
from typing import ClassVar
from uuid import UUID

from openhands.app_server.automation.execution_models import ExecutionState, SourceType
from openhands.app_server.automation.execution_store import ExecutionStore
from openhands.app_server.automation.input_sanitizer import has_dangerous_command
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
from openhands.sdk import Event
from openhands.sdk.event.hook_execution import HookExecutionEvent
from openhands.sdk.event.llm_convertible.action import ActionEvent

_logger = logging.getLogger(__name__)

COMMAND_REJECTION_MESSAGE = (
    '🚨 **Security Alert**: Layer 2 security has blocked a dangerous command '
    'and stopped the conversation.\n\n'
    '**Blocked command:**\n'
    '```\n'
    '{command}\n'
    '```\n\n'
    'The conversation has been stopped for safety. Please review the command '
    'and try again with appropriate safeguards.'
)

# ---------------------------------------------------------------------------
# Shared helpers used by both processors
# ---------------------------------------------------------------------------


async def _post_rejection_comment(record, command: str) -> None:
    """Post a security alert comment to the source (GitHub PR or Jira issue).

    Args:
        record: The execution record with source info.
        command: The blocked command to include in the message.
    """
    message = COMMAND_REJECTION_MESSAGE.format(command=command)

    try:
        if record.source_type == SourceType.GITHUB:
            if record.repository and record.github_pr_id:
                from openhands.app_server.utils.github import add_pr_comment

                add_pr_comment(record.repository, record.github_pr_id, message)
                _logger.info(
                    '[Security] Layer 2 rejection comment posted to '
                    '%s PR #%d',
                    record.repository,
                    record.github_pr_id,
                )

        elif record.source_type == SourceType.JIRA:
            if record.jira_issue_key:
                from openhands.app_server.utils.jira import add_comment

                add_comment(record.jira_issue_key, message)
                _logger.info(
                    '[Security] Layer 2 rejection comment posted to '
                    'Jira issue %s',
                    record.jira_issue_key,
                )
    except Exception:
        _logger.exception(
            '[Security] Failed to post Layer 2 rejection comment '
            'for execution %s',
            record.execution_id,
        )


async def _interrupt_conversation(conversation_id: UUID) -> None:
    """Interrupt a running conversation by calling the agent server.

    Uses deferred imports to avoid circular dependencies.
    """
    from openhands.app_server.config import (
        get_app_conversation_service,
        get_httpx_client,
    )
    from openhands.app_server.services.injector import InjectorState
    from openhands.app_server.user.specifiy_user_context import (
        ADMIN,
        USER_CONTEXT_ATTR,
    )
    from openhands.app_server.utils.docker_utils import (
        replace_localhost_hostname_for_docker,
    )

    state = InjectorState()
    setattr(state, USER_CONTEXT_ATTR, ADMIN)
    async with (
        get_app_conversation_service(state) as app_conversation_service,
        get_httpx_client(state) as httpx_client,
    ):
        app_conversation = await app_conversation_service.get_app_conversation(
            conversation_id
        )
        if not app_conversation:
            _logger.warning(
                '[Security] AppConversation not found for %s '
                '(cannot interrupt)',
                conversation_id,
            )
            return

        app_conversation_url = app_conversation.conversation_url
        if not app_conversation_url:
            _logger.warning(
                '[Security] No conversation URL for %s (cannot interrupt)',
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
                '[Security] Interrupted conversation %s (status=%d) '
                '(Layer 2 enforcement)',
                conversation_id,
                response.status_code,
            )
        except Exception:
            _logger.exception(
                '[Security] Failed to interrupt conversation %s',
                conversation_id,
            )


# ---------------------------------------------------------------------------
# Processor 1: ActionEvent path (command passed the hook but is dangerous)
# ---------------------------------------------------------------------------


class CommandEnforcementProcessor(EventCallbackProcessor):
    """Event callback processor that enforces dangerous command blocks.

    Registered on automation-triggered conversations. Listens for
    ActionEvent with ``tool_name='terminal'``, checks the command against
    ``block_dangerous.sh`` (the same PreToolUse hook script), and
    interrupts the conversation if a match is found.

    After interrupting, posts a security alert comment back to the source
    (GitHub PR or Jira issue) explaining which command was blocked.

    The companion ``block_dangerous.sh`` PreToolUse hook (see
    ``hook_files.py``) blocks commands at the runtime level *before*
    execution. This processor provides a secondary safety net for cases
    where the hook was not injected (e.g. pre-existing sandboxes).
    """

    event_kind: ClassVar[EventKind] = 'ActionEvent'

    async def __call__(
        self,
        conversation_id: UUID,
        callback: EventCallback,
        event: Event,
    ) -> EventCallbackResult | None:
        if not isinstance(event, ActionEvent):
            return None

        # Only check terminal commands
        if event.tool_name != 'terminal':
            return None

        # Extract the command from the action
        command = getattr(event.action, 'command', None)
        if not command:
            return None

        # Check against dangerous command patterns
        is_dangerous, matched_label = has_dangerous_command(command)
        if not is_dangerous:
            return None

        _logger.warning(
            '[Security] Layer 2 enforcement (ActionEvent) triggered for '
            'conversation %s: pattern=%s command=%r',
            conversation_id,
            matched_label,
            command[:500],
        )

        # Look up the execution record for source info
        store = ExecutionStore()
        record = await store.get_execution_by_conversation_id(
            str(conversation_id)
        )
        if not record:
            _logger.warning(
                '[Security] No execution record found for conversation %s '
                '(Layer 2 enforcement skipped)',
                conversation_id,
            )
            return None

        # Post security alert comment back to the source
        await _post_rejection_comment(record, command)

        # Interrupt the running conversation
        await _interrupt_conversation(conversation_id)

        # Update execution state to FAILED
        await store.update_state(
            execution_id=record.execution_id,
            state=ExecutionState.FAILED,
            error_message=(
                f'Dangerous command blocked (Layer 2): '
                f'{matched_label}: {command[:200]}'
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


# ---------------------------------------------------------------------------
# Processor 2: HookExecutionEvent path (the PreToolUse hook blocked the
# command before it could execute — ``block_dangerous.sh`` exited with
# code 2).  This is the primary enforcement path.
# ---------------------------------------------------------------------------


class BlockedHookProcessor(EventCallbackProcessor):
    """Event callback processor that detects commands denied by the
    ``block_dangerous.sh`` PreToolUse hook.

    When the hook fires and blocks a command (exit code 2), the agent
    server emits a ``HookExecutionEvent`` with ``blocked=True`` and
    ``exit_code=2``.  This processor catches those events and posts a
    security alert comment to GitHub/Jira, interrupts the conversation,
    and marks the execution as FAILED.

    This is the **primary** Layer 2 enforcement path — the hook blocks
    the command *before* it can execute, and this processor handles the
    post-hoc notification and conversation cleanup.
    """

    event_kind: ClassVar[EventKind] = 'HookExecutionEvent'

    async def __call__(
        self,
        conversation_id: UUID,
        callback: EventCallback,
        event: Event,
    ) -> EventCallbackResult | None:
        if not isinstance(event, HookExecutionEvent):
            return None

        # Only care about PreToolUse hook events that blocked
        if (
            event.hook_event_type != 'PreToolUse'
            or not event.blocked
            or event.exit_code != 2
        ):
            return None

        command = event.hook_command or ''
        reason = event.reason or 'dangerous command blocked'

        _logger.warning(
            '[Security] Layer 2 enforcement (blocked hook) triggered for '
            'conversation %s: reason=%s command=%r',
            conversation_id,
            reason,
            command[:500],
        )

        # Look up the execution record for source info
        store = ExecutionStore()
        record = await store.get_execution_by_conversation_id(
            str(conversation_id)
        )
        if not record:
            _logger.warning(
                '[Security] No execution record found for conversation %s '
                '(BlockedHookProcessor skipped)',
                conversation_id,
            )
            return None

        # Post security alert comment back to the source
        await _post_rejection_comment(record, command)

        # Interrupt the running conversation
        await _interrupt_conversation(conversation_id)

        # Update execution state to FAILED
        await store.update_state(
            execution_id=record.execution_id,
            state=ExecutionState.FAILED,
            error_message=(
                f'Dangerous command blocked (Layer 2): '
                f'{reason}: {command[:200]}'
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
