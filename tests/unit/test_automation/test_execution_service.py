"""Tests for ExecutionService."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from openhands.app_server.automation.execution_models import (
    ExecutionRecord,
    ExecutionState,
    SourceType,
)
from openhands.app_server.automation.execution_service import ExecutionService


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.create_execution = AsyncMock()
    store.update_state = AsyncMock()
    store.get_execution = AsyncMock()
    store.get_execution_by_source_event = AsyncMock()
    return store


@pytest.fixture
def execution_service(mock_store):
    return ExecutionService(store=mock_store)


class TestCreateExecution:
    async def test_creates_new_execution(self, execution_service, mock_store):
        mock_store.get_execution_by_source_event.return_value = None
        mock_store.create_execution.return_value = ExecutionRecord(
            execution_id='exec_test123',
            state=ExecutionState.RECEIVED,
        )

        record, is_new = await execution_service.create_execution(
            source_type=SourceType.JIRA,
            source_event_id='event_123',
            jira_issue_key='KAN-17',
        )

        assert is_new is True
        assert record.execution_id == 'exec_test123'
        mock_store.create_execution.assert_called_once()

    async def test_returns_existing_on_duplicate(
        self, execution_service, mock_store
    ):
        mock_store.get_execution_by_source_event.return_value = ExecutionRecord(
            execution_id='exec_existing',
            state=ExecutionState.RECEIVED,
        )

        record, is_new = await execution_service.create_execution(
            source_type=SourceType.JIRA,
            source_event_id='event_123',
        )

        assert is_new is False
        assert record.execution_id == 'exec_existing'
        mock_store.create_execution.assert_not_called()

    async def test_handles_no_event_id(self, execution_service, mock_store):
        mock_store.create_execution.return_value = ExecutionRecord(
            execution_id='exec_test', state=ExecutionState.RECEIVED
        )

        record, is_new = await execution_service.create_execution(
            source_type=SourceType.GITHUB,
        )

        assert is_new is True
        mock_store.get_execution_by_source_event.assert_not_called()


class TestTransitionState:
    async def test_valid_transition(self, execution_service, mock_store):
        mock_store.get_execution.return_value = ExecutionRecord(
            execution_id='exec_test',
            state=ExecutionState.RECEIVED,
        )
        mock_store.update_state.return_value = ExecutionRecord(
            execution_id='exec_test',
            state=ExecutionState.QUEUED,
        )

        result = await execution_service.transition_state(
            'exec_test', ExecutionState.QUEUED
        )

        assert result is not None
        mock_store.update_state.assert_called_once_with(
            execution_id='exec_test',
            state=ExecutionState.QUEUED,
            error_message=None,
            conversation_id=None,
        )

    async def test_invalid_transition(self, execution_service, mock_store):
        mock_store.get_execution.return_value = ExecutionRecord(
            execution_id='exec_test',
            state=ExecutionState.RECEIVED,
        )

        with pytest.raises(ValueError, match='Invalid state transition'):
            await execution_service.transition_state(
                'exec_test', ExecutionState.COMPLETED
            )

        mock_store.update_state.assert_not_called()

    async def test_execution_not_found(self, execution_service, mock_store):
        mock_store.get_execution.return_value = None

        result = await execution_service.transition_state(
            'exec_nonexistent', ExecutionState.RUNNING
        )

        assert result is None
        mock_store.update_state.assert_not_called()
