"""Tests for AutomationEventCallbackProcessor."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from openhands.app_server.automation.callback_processors import (
    AutomationEventCallbackProcessor,
)
from openhands.app_server.automation.execution_models import (
    ExecutionRecord,
    ExecutionState,
)
from openhands.app_server.event_callback.event_callback_models import (
    EventCallback,
    EventCallbackStatus,
)
from openhands.sdk.conversation import ConversationExecutionStatus
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent
from openhands.sdk.llm.utils.metrics import MetricsSnapshot, TokenUsage


@asynccontextmanager
async def _ctx(obj):
    yield obj


def _make_event(exec_status: ConversationExecutionStatus) -> ConversationStateUpdateEvent:
    return ConversationStateUpdateEvent(
        key='execution_status',
        value=exec_status.value,
    )


def _make_callback(callback_id: str | None = None) -> EventCallback:
    return EventCallback(
        conversation_id=uuid4(),
        processor=AutomationEventCallbackProcessor(),
        id=UUID(callback_id) if callback_id else uuid4(),
    )


@pytest.mark.asyncio
async def test_non_terminal_event_returns_none():
    """Non-terminal execution status events should be ignored."""
    processor = AutomationEventCallbackProcessor()
    event = _make_event(ConversationExecutionStatus.RUNNING)
    callback = _make_callback()

    result = await processor(uuid4(), callback, event)

    assert result is None


@pytest.mark.asyncio
async def test_no_execution_record_returns_none():
    """Terminal event without a matching execution record should return None."""
    processor = AutomationEventCallbackProcessor()
    event = _make_event(ConversationExecutionStatus.FINISHED)
    callback = _make_callback()

    with patch(
        'openhands.app_server.automation.callback_processors.ExecutionStore'
    ) as MockStore:
        mock_store = MagicMock()
        mock_store.get_execution_by_conversation_id = AsyncMock(return_value=None)
        MockStore.return_value = mock_store

        result = await processor(uuid4(), callback, event)

    assert result is None
    assert callback.status == EventCallbackStatus.ACTIVE


@pytest.mark.asyncio
async def test_finished_event_updates_state():
    """FINISHED event should update execution to COMPLETED."""
    processor = AutomationEventCallbackProcessor()
    conversation_id = uuid4()
    event = _make_event(ConversationExecutionStatus.FINISHED)
    callback = _make_callback()

    execution_record = ExecutionRecord(
        execution_id='exec_test_123',
        state=ExecutionState.RUNNING,
        jira_issue_key=None,  # No JIRA key - skip token usage
    )

    mock_store = MagicMock()
    mock_store.get_execution_by_conversation_id = AsyncMock(
        return_value=execution_record
    )
    mock_store.update_state = AsyncMock()

    with patch(
        'openhands.app_server.automation.callback_processors.ExecutionStore',
        return_value=mock_store,
    ):
        result = await processor(conversation_id, callback, event)

    assert result is not None
    assert result.status.value == 'SUCCESS'
    mock_store.update_state.assert_called_once_with(
        execution_id='exec_test_123',
        state=ExecutionState.COMPLETED,
        conversation_id=str(conversation_id),
    )
    assert callback.status == EventCallbackStatus.COMPLETED


@pytest.mark.asyncio
async def test_error_event_updates_state():
    """ERROR event should update execution to FAILED."""
    processor = AutomationEventCallbackProcessor()
    conversation_id = uuid4()
    event = _make_event(ConversationExecutionStatus.ERROR)
    callback = _make_callback()

    execution_record = ExecutionRecord(
        execution_id='exec_test_err',
        state=ExecutionState.RUNNING,
    )

    mock_store = MagicMock()
    mock_store.get_execution_by_conversation_id = AsyncMock(
        return_value=execution_record
    )
    mock_store.update_state = AsyncMock()

    with patch(
        'openhands.app_server.automation.callback_processors.ExecutionStore',
        return_value=mock_store,
    ):
        result = await processor(conversation_id, callback, event)

    assert result is not None
    mock_store.update_state.assert_called_once_with(
        execution_id='exec_test_err',
        state=ExecutionState.FAILED,
        conversation_id=str(conversation_id),
    )


@pytest.mark.asyncio
async def test_jira_token_usage_posted_on_completion():
    """JIRA-triggered FINISHED event should post token usage comment."""
    processor = AutomationEventCallbackProcessor()
    conversation_id = uuid4()
    event = _make_event(ConversationExecutionStatus.FINISHED)
    callback = _make_callback()

    execution_record = ExecutionRecord(
        execution_id='exec_jira_1',
        state=ExecutionState.RUNNING,
        jira_issue_key='KAN-99',
        max_budget=10.0,
    )

    mock_store = MagicMock()
    mock_store.get_execution_by_conversation_id = AsyncMock(
        return_value=execution_record
    )
    mock_store.update_state = AsyncMock()

    metrics = MetricsSnapshot(
        model_name='gpt-4',
        accumulated_cost=0.002345,
        accumulated_token_usage=TokenUsage(
            model='gpt-4',
            prompt_tokens=150,
            completion_tokens=50,
            cache_read_tokens=20,
        ),
    )

    mock_conv_info = MagicMock()
    mock_conv_info.metrics = metrics

    mock_info_service = AsyncMock()
    mock_info_service.get_app_conversation_info = AsyncMock(
        return_value=mock_conv_info
    )

    with (
        patch(
            'openhands.app_server.automation.callback_processors.ExecutionStore',
            return_value=mock_store,
        ),
        patch(
            'openhands.app_server.automation.callback_processors'
            '.get_app_conversation_info_service',
            return_value=_ctx(mock_info_service),
        ),
        patch(
            'openhands.app_server.utils.jira.add_comment',
            return_value={'id': '12345'},
        ) as mock_add_comment,
    ):
        await processor(conversation_id, callback, event)

    # Verify the JIRA comment was posted with token usage
    mock_add_comment.assert_called_once()
    args, _ = mock_add_comment.call_args
    assert args[0] == 'KAN-99'  # issue_key
    body = args[1]
    assert '*Total Cost:* $0.002345' in body
    assert '*Model:* gpt-4' in body
    assert '- Prompt tokens: 150' in body
    assert '- Completion tokens: 50' in body
    assert '- Total tokens: 200' in body
    assert '- Cache read tokens: 20' in body
    assert '*Budget Usage:* $0.0023 / $10.0000' in body


@pytest.mark.asyncio
async def test_jira_token_usage_skipped_on_failure():
    """FAILED conversations with JIRA key should NOT post token usage."""
    processor = AutomationEventCallbackProcessor()
    conversation_id = uuid4()
    event = _make_event(ConversationExecutionStatus.ERROR)
    callback = _make_callback()

    execution_record = ExecutionRecord(
        execution_id='exec_failed',
        state=ExecutionState.RUNNING,
        jira_issue_key='KAN-99',
    )

    mock_store = MagicMock()
    mock_store.get_execution_by_conversation_id = AsyncMock(
        return_value=execution_record
    )
    mock_store.update_state = AsyncMock()

    with (
        patch(
            'openhands.app_server.automation.callback_processors.ExecutionStore',
            return_value=mock_store,
        ),
        patch(
            'openhands.app_server.automation.callback_processors'
            '.AutomationEventCallbackProcessor._post_jira_token_usage'
        ) as mock_post,
    ):
        await processor(conversation_id, callback, event)

    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_jira_token_usage_skipped_without_jira_key():
    """Non-JIRA conversations should skip token usage posting."""
    processor = AutomationEventCallbackProcessor()
    conversation_id = uuid4()
    event = _make_event(ConversationExecutionStatus.FINISHED)
    callback = _make_callback()

    execution_record = ExecutionRecord(
        execution_id='exec_no_jira',
        state=ExecutionState.RUNNING,
        jira_issue_key=None,
    )

    mock_store = MagicMock()
    mock_store.get_execution_by_conversation_id = AsyncMock(
        return_value=execution_record
    )
    mock_store.update_state = AsyncMock()

    with (
        patch(
            'openhands.app_server.automation.callback_processors.ExecutionStore',
            return_value=mock_store,
        ),
        patch(
            'openhands.app_server.automation.callback_processors'
            '.AutomationEventCallbackProcessor._post_jira_token_usage'
        ) as mock_post,
    ):
        await processor(conversation_id, callback, event)

    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_jira_token_usage_handles_empty_metrics():
    """No metrics should skip posting and log info."""
    processor = AutomationEventCallbackProcessor()
    conversation_id = uuid4()
    event = _make_event(ConversationExecutionStatus.FINISHED)
    callback = _make_callback()

    execution_record = ExecutionRecord(
        execution_id='exec_no_metrics',
        state=ExecutionState.RUNNING,
        jira_issue_key='KAN-99',
    )

    mock_store = MagicMock()
    mock_store.get_execution_by_conversation_id = AsyncMock(
        return_value=execution_record
    )
    mock_store.update_state = AsyncMock()

    mock_conv_info = MagicMock()
    mock_conv_info.metrics = None

    mock_info_service = AsyncMock()
    mock_info_service.get_app_conversation_info = AsyncMock(
        return_value=mock_conv_info
    )

    with (
        patch(
            'openhands.app_server.automation.callback_processors.ExecutionStore',
            return_value=mock_store,
        ),
        patch(
            'openhands.app_server.automation.callback_processors'
            '.get_app_conversation_info_service',
            return_value=_ctx(mock_info_service),
        ),
        patch(
            'openhands.app_server.utils.jira.add_comment'
        ) as mock_add_comment,
    ):
        await processor(conversation_id, callback, event)

    mock_add_comment.assert_not_called()
