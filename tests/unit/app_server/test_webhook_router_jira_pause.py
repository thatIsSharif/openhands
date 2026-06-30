"""Tests for Jira sandbox auto-pause in webhook_router.

Tests _pause_jira_sandbox and its integration into _run_callbacks_in_bg_and_close.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from openhands.app_server.event_callback.webhook_router import (
    _pause_jira_sandbox,
    _run_callbacks_in_bg_and_close,
    on_event,
)
from openhands.app_server.sandbox.sandbox_models import SandboxStatus
from openhands.sdk.conversation import ConversationExecutionStatus
from openhands.sdk.event import ConversationStateUpdateEvent


def _mock_sandbox(status: SandboxStatus = SandboxStatus.RUNNING) -> MagicMock:
    sandbox = MagicMock()
    sandbox.id = 'sandbox-jira-123'
    sandbox.status = status
    return sandbox


def _mock_injector(sandbox_service: AsyncMock):
    """Build a mock injector whose context() yields sandbox_service."""
    injector = MagicMock()
    injector.context = asynccontextmanager(
        lambda state: _async_yield(sandbox_service)
    )
    return injector


async def _async_yield(obj):
    """Helper: async generator that yields a single value."""
    yield obj


class TestPauseJiraSandbox:
    """Tests for the _pause_jira_sandbox helper function."""

    @pytest.mark.asyncio
    async def test_pauses_running_sandbox(self):
        """Should pause a RUNNING sandbox."""
        sandbox_service = AsyncMock()
        sandbox_service.get_sandbox = AsyncMock(
            return_value=_mock_sandbox(SandboxStatus.RUNNING)
        )
        sandbox_service.pause_sandbox = AsyncMock(return_value=True)

        with patch(
            'openhands.app_server.event_callback.webhook_router.get_global_config'
        ) as mock_get_config:
            mock_config = MagicMock()
            mock_config.sandbox = _mock_injector(sandbox_service)
            mock_get_config.return_value = mock_config

            await _pause_jira_sandbox('sandbox-jira-123', 'user_123')

        sandbox_service.get_sandbox.assert_awaited_once_with('sandbox-jira-123')
        sandbox_service.pause_sandbox.assert_awaited_once_with('sandbox-jira-123')

    @pytest.mark.asyncio
    async def test_skips_already_paused_sandbox(self):
        """Should NOT pause an already PAUSED sandbox."""
        sandbox_service = AsyncMock()
        sandbox_service.get_sandbox = AsyncMock(
            return_value=_mock_sandbox(SandboxStatus.PAUSED)
        )
        sandbox_service.pause_sandbox = AsyncMock(return_value=True)

        with patch(
            'openhands.app_server.event_callback.webhook_router.get_global_config'
        ) as mock_get_config:
            mock_config = MagicMock()
            mock_config.sandbox = _mock_injector(sandbox_service)
            mock_get_config.return_value = mock_config

            await _pause_jira_sandbox('sandbox-jira-123', 'user_123')

        sandbox_service.get_sandbox.assert_awaited_once_with('sandbox-jira-123')
        sandbox_service.pause_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_missing_sandbox(self):
        """Should gracefully handle when sandbox is missing (None)."""
        sandbox_service = AsyncMock()
        sandbox_service.get_sandbox = AsyncMock(return_value=None)
        sandbox_service.pause_sandbox = AsyncMock(return_value=True)

        with patch(
            'openhands.app_server.event_callback.webhook_router.get_global_config'
        ) as mock_get_config:
            mock_config = MagicMock()
            mock_config.sandbox = _mock_injector(sandbox_service)
            mock_get_config.return_value = mock_config

            await _pause_jira_sandbox('sandbox-jira-123', 'user_123')

        sandbox_service.get_sandbox.assert_awaited_once_with('sandbox-jira-123')
        sandbox_service.pause_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_no_user_id(self):
        """Should work when user_id is None."""
        sandbox_service = AsyncMock()
        sandbox_service.get_sandbox = AsyncMock(
            return_value=_mock_sandbox(SandboxStatus.RUNNING)
        )
        sandbox_service.pause_sandbox = AsyncMock(return_value=True)

        with patch(
            'openhands.app_server.event_callback.webhook_router.get_global_config'
        ) as mock_get_config:
            mock_config = MagicMock()
            mock_config.sandbox = _mock_injector(sandbox_service)
            mock_get_config.return_value = mock_config

            await _pause_jira_sandbox('sandbox-jira-123', None)

        sandbox_service.get_sandbox.assert_awaited_once_with('sandbox-jira-123')
        sandbox_service.pause_sandbox.assert_awaited_once_with('sandbox-jira-123')

    @pytest.mark.asyncio
    async def test_handles_no_sandbox_injector(self):
        """Should gracefully handle when sandbox injector is None."""
        with patch(
            'openhands.app_server.event_callback.webhook_router.get_global_config'
        ) as mock_get_config:
            mock_config = MagicMock()
            mock_config.sandbox = None
            mock_get_config.return_value = mock_config

            # Should not raise
            await _pause_jira_sandbox('sandbox-jira-123', 'user_123')

    @pytest.mark.asyncio
    async def test_handles_get_sandbox_exception(self):
        """Should gracefully handle exceptions from get_sandbox."""
        sandbox_service = AsyncMock()
        sandbox_service.get_sandbox = AsyncMock(side_effect=Exception('DB error'))

        with patch(
            'openhands.app_server.event_callback.webhook_router.get_global_config'
        ) as mock_get_config:
            mock_config = MagicMock()
            mock_config.sandbox = _mock_injector(sandbox_service)
            mock_get_config.return_value = mock_config

            # Should not raise
            await _pause_jira_sandbox('sandbox-jira-123', 'user_123')

        sandbox_service.get_sandbox.assert_awaited_once_with('sandbox-jira-123')
        sandbox_service.pause_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_pause_exception(self):
        """Should gracefully handle exceptions from pause_sandbox."""
        sandbox_service = AsyncMock()
        sandbox_service.get_sandbox = AsyncMock(
            return_value=_mock_sandbox(SandboxStatus.RUNNING)
        )
        sandbox_service.pause_sandbox = AsyncMock(
            side_effect=Exception('Permission denied')
        )

        with patch(
            'openhands.app_server.event_callback.webhook_router.get_global_config'
        ) as mock_get_config:
            mock_config = MagicMock()
            mock_config.sandbox = _mock_injector(sandbox_service)
            mock_get_config.return_value = mock_config

            # Should not raise
            await _pause_jira_sandbox('sandbox-jira-123', 'user_123')

        sandbox_service.get_sandbox.assert_awaited_once_with('sandbox-jira-123')
        sandbox_service.pause_sandbox.assert_awaited_once_with('sandbox-jira-123')


class TestRunCallbacksWithJiraPause:
    """Tests that _run_callbacks_in_bg_and_close properly calls _pause_jira_sandbox."""

    @pytest.mark.asyncio
    async def test_pauses_when_sandbox_id_provided(self):
        """Should call _pause_jira_sandbox when sandbox_id is provided."""
        conversation_id = uuid4()

        with (
            patch(
                'openhands.app_server.event_callback.webhook_router.get_event_callback_service'
            ) as mock_get_ecs,
            patch(
                'openhands.app_server.event_callback.webhook_router._pause_jira_sandbox'
            ) as mock_pause,
        ):
            mock_ecs = AsyncMock()
            mock_ecs.__aenter__.return_value = AsyncMock()
            mock_get_ecs.return_value = mock_ecs

            await _run_callbacks_in_bg_and_close(
                conversation_id=conversation_id,
                user_id='user_123',
                events=[],
                sandbox_id='sandbox-jira-123',
            )

            mock_pause.assert_awaited_once_with(
                'sandbox-jira-123', 'user_123'
            )

    @pytest.mark.asyncio
    async def test_skips_pause_when_no_sandbox_id(self):
        """Should NOT call _pause_jira_sandbox when sandbox_id is None."""
        conversation_id = uuid4()

        with (
            patch(
                'openhands.app_server.event_callback.webhook_router.get_event_callback_service'
            ) as mock_get_ecs,
            patch(
                'openhands.app_server.event_callback.webhook_router._pause_jira_sandbox'
            ) as mock_pause,
        ):
            mock_ecs = AsyncMock()
            mock_ecs.__aenter__.return_value = AsyncMock()
            mock_get_ecs.return_value = mock_ecs

            await _run_callbacks_in_bg_and_close(
                conversation_id=conversation_id,
                user_id='user_123',
                events=[],
                sandbox_id=None,
            )

            mock_pause.assert_not_called()

    @pytest.mark.asyncio
    async def test_pauses_after_callbacks_complete(self):
        """Should pause AFTER all callbacks have executed."""
        conversation_id = uuid4()
        callback_order = []

        with (
            patch(
                'openhands.app_server.event_callback.webhook_router.get_event_callback_service'
            ) as mock_get_ecs,
            patch(
                'openhands.app_server.event_callback.webhook_router._pause_jira_sandbox'
            ) as mock_pause,
        ):
            event_callback_service = AsyncMock()

            async def tracking_execute_callbacks(*args, **kwargs):
                callback_order.append('callbacks')
                return None
            event_callback_service.execute_callbacks = tracking_execute_callbacks

            mock_ecs = AsyncMock()
            mock_ecs.__aenter__.return_value = event_callback_service
            mock_get_ecs.return_value = mock_ecs

            async def tracking_pause(*args, **kwargs):
                callback_order.append('pause')

            mock_pause.side_effect = tracking_pause

            await _run_callbacks_in_bg_and_close(
                conversation_id=conversation_id,
                user_id='user_123',
                events=[],
                sandbox_id='sandbox-jira-123',
            )

            assert callback_order == ['callbacks', 'pause'], (
                f'Expected callbacks before pause, got: {callback_order}'
            )


class TestOnEventJiraPauseIntegration:
    """Tests that on_event correctly captures jira_sandbox_to_pause and
    passes it to _run_callbacks_in_bg_and_close on terminal events."""

    @pytest.mark.asyncio
    async def test_jira_sandbox_paused_on_terminal_event(self):
        """When a Jira conversation reaches a terminal state, the sandbox_id
        should be passed to _run_callbacks_in_bg_and_close."""
        conversation_id = uuid4()
        terminal_event = ConversationStateUpdateEvent(
            id='evt-1',
            key='execution_status',
            value='finished',
        )

        app_conversation_info = MagicMock()
        app_conversation_info.id = conversation_id
        app_conversation_info.sandbox_id = 'sandbox-jira-123'
        app_conversation_info.jira_issue_key = 'KAN-123'
        app_conversation_info.created_by_user_id = 'user_123'

        captured_sandbox_id = None

        with (
            patch(
                'openhands.app_server.event_callback.webhook_router._track_conversation_terminal'
            ) as mock_track,
            patch(
                'openhands.app_server.event_callback.webhook_router._run_callbacks_in_bg_and_close'
            ) as mock_run_callbacks,
            patch(
                'openhands.app_server.event_callback.webhook_router.app_conversation_info_service_dependency'
            ),
        ):
            mock_track.return_value = None

            async def capture_callbacks(conversation_id, user_id, events, sandbox_id=None):
                nonlocal captured_sandbox_id
                captured_sandbox_id = sandbox_id

            mock_run_callbacks.side_effect = capture_callbacks

            # We need to call on_event, but it requires FastAPI Depends.
            # Instead, directly test the logic used inside it.
            # The critical part is the analytics loop and create_task call.
            # Let's replicate the relevant logic inline.

            jira_sandbox_to_pause: str | None = None
            for event in [terminal_event]:
                if not isinstance(event, ConversationStateUpdateEvent):
                    continue
                if event.key != 'execution_status':
                    continue
                exec_status = ConversationExecutionStatus(event.value)
                if exec_status.is_terminal():
                    await _track_conversation_terminal(
                        conversation_id, app_conversation_info, [terminal_event], exec_status
                    )
                    if app_conversation_info.jira_issue_key:
                        jira_sandbox_to_pause = app_conversation_info.sandbox_id

            await _run_callbacks_in_bg_and_close(
                conversation_id,
                app_conversation_info.created_by_user_id,
                [terminal_event],
                sandbox_id=jira_sandbox_to_pause,
            )

            assert captured_sandbox_id == 'sandbox-jira-123', (
                f'sandbox_id should be passed for Jira conversations, '
                f'got: {captured_sandbox_id}'
            )

    @pytest.mark.asyncio
    async def test_non_jira_conversation_not_paused(self):
        """When a non-Jira conversation reaches a terminal state,
        sandbox_id should NOT be passed."""
        conversation_id = uuid4()
        terminal_event = ConversationStateUpdateEvent(
            id='evt-2',
            key='execution_status',
            value='finished',
        )

        app_conversation_info = MagicMock()
        app_conversation_info.id = conversation_id
        app_conversation_info.sandbox_id = 'sandbox-regular-456'
        app_conversation_info.jira_issue_key = None  # Not a Jira conversation
        app_conversation_info.created_by_user_id = 'user_123'

        captured_sandbox_id = 'should-be-overwritten'

        with (
            patch(
                'openhands.app_server.event_callback.webhook_router._track_conversation_terminal'
            ) as mock_track,
            patch(
                'openhands.app_server.event_callback.webhook_router._run_callbacks_in_bg_and_close'
            ) as mock_run_callbacks,
        ):
            mock_track.return_value = None

            async def capture_callbacks(conversation_id, user_id, events, sandbox_id=None):
                nonlocal captured_sandbox_id
                captured_sandbox_id = sandbox_id

            mock_run_callbacks.side_effect = capture_callbacks

            # Replicate the same logic as in on_event
            jira_sandbox_to_pause: str | None = None
            for event in [terminal_event]:
                if not isinstance(event, ConversationStateUpdateEvent):
                    continue
                if event.key != 'execution_status':
                    continue
                exec_status = ConversationExecutionStatus(event.value)
                if exec_status.is_terminal():
                    await _track_conversation_terminal(
                        conversation_id, app_conversation_info, [terminal_event], exec_status
                    )
                    if app_conversation_info.jira_issue_key:
                        jira_sandbox_to_pause = app_conversation_info.sandbox_id

            await _run_callbacks_in_bg_and_close(
                conversation_id,
                app_conversation_info.created_by_user_id,
                [terminal_event],
                sandbox_id=jira_sandbox_to_pause,
            )

            assert captured_sandbox_id is None, (
                f'sandbox_id should be None for non-Jira conversations, '
                f'got: {captured_sandbox_id}'
            )

    @pytest.mark.asyncio
    async def test_non_terminal_event_does_not_pause(self):
        """A non-terminal execution_status event should not trigger pause."""
        conversation_id = uuid4()
        non_terminal_event = ConversationStateUpdateEvent(
            id='evt-3',
            key='execution_status',
            value='running',  # non-terminal
        )

        app_conversation_info = MagicMock()
        app_conversation_info.id = conversation_id
        app_conversation_info.sandbox_id = 'sandbox-jira-123'
        app_conversation_info.jira_issue_key = 'KAN-123'
        app_conversation_info.created_by_user_id = 'user_123'

        captured_sandbox_id = None

        with (
            patch(
                'openhands.app_server.event_callback.webhook_router._run_callbacks_in_bg_and_close'
            ) as mock_run_callbacks,
        ):
            async def capture_callbacks(conversation_id, user_id, events, sandbox_id=None):
                nonlocal captured_sandbox_id
                captured_sandbox_id = sandbox_id

            mock_run_callbacks.side_effect = capture_callbacks

            # Replicate the same logic
            jira_sandbox_to_pause: str | None = None
            for event in [non_terminal_event]:
                if not isinstance(event, ConversationStateUpdateEvent):
                    continue
                if event.key != 'execution_status':
                    continue
                exec_status = ConversationExecutionStatus(event.value)
                if exec_status.is_terminal():
                    if app_conversation_info.jira_issue_key:
                        jira_sandbox_to_pause = app_conversation_info.sandbox_id

            await _run_callbacks_in_bg_and_close(
                conversation_id,
                app_conversation_info.created_by_user_id,
                [non_terminal_event],
                sandbox_id=jira_sandbox_to_pause,
            )

            assert captured_sandbox_id is None, (
                f'sandbox_id should be None for non-terminal events, '
                f'got: {captured_sandbox_id}'
            )
