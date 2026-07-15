"""Tests for AutomationEventCallbackProcessor.

The simplified callback processor no longer interacts with ExecutionStore;
it simply logs terminal state transitions for observability.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from openhands.app_server.automation.callback_processors import (
    AutomationEventCallbackProcessor,
)
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent


@pytest.fixture
def processor():
    return AutomationEventCallbackProcessor()


@pytest.fixture
def conversation_id():
    return uuid4()


@pytest.fixture
def callback():
    cb = MagicMock()
    cb.id = uuid4()
    cb.status = 'active'
    return cb


def _make_event(key: str, value: str):
    """Helper to create a ConversationStateUpdateEvent."""
    event = MagicMock(spec=ConversationStateUpdateEvent)
    event.key = key
    event.value = value
    event.id = str(uuid4())
    return event


@pytest.mark.asyncio
async def test_non_state_update_event(processor, conversation_id, callback):
    """Non-ConversationStateUpdateEvent returns None."""
    event = MagicMock()
    event.key = 'execution_status'
    event.value = 'finished'

    result = await processor(conversation_id, callback, event)
    assert result is None


@pytest.mark.asyncio
async def test_non_execution_status_key(processor, conversation_id, callback):
    """Events with non-execution_status key return None."""
    event = _make_event('some_other_key', 'finished')

    result = await processor(conversation_id, callback, event)
    assert result is None


@pytest.mark.asyncio
async def test_non_terminal_status(processor, conversation_id, callback):
    """Non-terminal status events return None."""
    event = _make_event('execution_status', 'running')

    result = await processor(conversation_id, callback, event)
    assert result is None


@pytest.mark.asyncio
async def test_terminal_finished_returns_success(
    processor, conversation_id, callback,
):
    """Terminal FINISHED returns EventCallbackResult with SUCCESS."""
    event = _make_event('execution_status', 'finished')

    result = await processor(conversation_id, callback, event)

    assert result is not None
    assert result.status.value == 'SUCCESS'
    # Callback should be marked COMPLETED
    assert callback.status.value == 'COMPLETED'


@pytest.mark.asyncio
async def test_terminal_error_returns_success(
    processor, conversation_id, callback,
):
    """Terminal ERROR also returns SUCCESS (just logs the transition)."""
    event = _make_event('execution_status', 'error')

    result = await processor(conversation_id, callback, event)

    assert result is not None
    assert result.status.value == 'SUCCESS'
