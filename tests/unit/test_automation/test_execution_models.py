"""Tests for execution models and state machine."""

import pytest

from openhands.app_server.automation.execution_models import (
    ExecutionState,
    SourceType,
    validate_transition,
)


class TestExecutionState:
    def test_received_to_queued(self):
        """RECEIVED \u2192 QUEUED is valid."""
        validate_transition(ExecutionState.RECEIVED, ExecutionState.QUEUED)

    def test_received_to_cancelled(self):
        """RECEIVED \u2192 CANCELLED is valid."""
        validate_transition(ExecutionState.RECEIVED, ExecutionState.CANCELLED)

    def test_queued_to_running(self):
        """QUEUED \u2192 RUNNING is valid."""
        validate_transition(ExecutionState.QUEUED, ExecutionState.RUNNING)

    def test_queued_to_cancelled(self):
        """QUEUED \u2192 CANCELLED is valid."""
        validate_transition(ExecutionState.QUEUED, ExecutionState.CANCELLED)

    def test_running_to_completed(self):
        """RUNNING \u2192 COMPLETED is valid."""
        validate_transition(ExecutionState.RUNNING, ExecutionState.COMPLETED)

    def test_running_to_failed(self):
        """RUNNING \u2192 FAILED is valid."""
        validate_transition(ExecutionState.RUNNING, ExecutionState.FAILED)

    def test_running_to_cancelled(self):
        """RUNNING \u2192 CANCELLED is valid."""
        validate_transition(ExecutionState.RUNNING, ExecutionState.CANCELLED)

    def test_invalid_transition_from_completed(self):
        """COMPLETED has no valid transitions."""
        for target in ExecutionState:
            if target != ExecutionState.COMPLETED:
                with pytest.raises(ValueError, match='Invalid state transition'):
                    validate_transition(ExecutionState.COMPLETED, target)

    def test_invalid_transition_received_to_completed(self):
        """RECEIVED \u2192 COMPLETED is invalid (skips QUEUED and RUNNING)."""
        with pytest.raises(ValueError, match='Invalid state transition'):
            validate_transition(ExecutionState.RECEIVED, ExecutionState.COMPLETED)

    def test_invalid_transition_queued_to_completed(self):
        """QUEUED \u2192 COMPLETED is invalid (skips RUNNING)."""
        with pytest.raises(ValueError, match='Invalid state transition'):
            validate_transition(ExecutionState.QUEUED, ExecutionState.COMPLETED)

    def test_all_states_have_enum_values(self):
        """All execution states have string values."""
        assert ExecutionState.RECEIVED.value == 'RECEIVED'
        assert ExecutionState.QUEUED.value == 'QUEUED'
        assert ExecutionState.RUNNING.value == 'RUNNING'
        assert ExecutionState.COMPLETED.value == 'COMPLETED'
        assert ExecutionState.FAILED.value == 'FAILED'
        assert ExecutionState.CANCELLED.value == 'CANCELLED'


class TestSourceType:
    def test_source_type_values(self):
        assert SourceType.JIRA.value == 'jira'
        assert SourceType.GITHUB.value == 'github'
