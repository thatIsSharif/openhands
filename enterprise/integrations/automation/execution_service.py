"""Execution service - manages the execution lifecycle state machine.

Provides:
- Execution creation with idempotency checking
- State transition validation and persistence
- Execution history querying
"""

from __future__ import annotations

from datetime import datetime, timezone

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context, generate_execution_id
from .execution_models import (
    ExecutionRecord,
    ExecutionState,
    SourceType,
    validate_transition,
)
from .execution_store import ExecutionStore


class ExecutionService:
    """Manages the automation execution lifecycle."""

    def __init__(self, store: ExecutionStore | None = None) -> None:
        self._store = store or ExecutionStore()

    async def create_execution(
        self,
        source_type: SourceType | str,
        source_event_id: str | None = None,
        jira_issue_key: str | None = None,
        github_pr_id: int | None = None,
        repository: str | None = None,
        branch: str | None = None,
    ) -> tuple[ExecutionRecord, bool]:
        """Create a new execution with idempotency checking.

        Returns:
            Tuple of (execution_record, is_new).
            is_new is True if this is a new execution, False if duplicate.
        """
        # Idempotency check
        if source_event_id:
            existing = await self._store.get_execution_by_source_event(
                source_event_id
            )
            if existing:
                logger.info(
                    f'[Automation] Duplicate execution detected: '
                    f'source_event_id={source_event_id}, '
                    f'existing_execution={existing.execution_id}'
                )
                return existing, False

        execution_id = generate_execution_id()
        log_ctx = build_log_context(
            execution_id=execution_id,
            repository=repository,
            branch=branch,
            jira_issue_key=jira_issue_key,
            pr_number=github_pr_id,
        )

        record = await self._store.create_execution(
            execution_id=execution_id,
            source_type=source_type,
            source_event_id=source_event_id,
            jira_issue_key=jira_issue_key,
            github_pr_id=github_pr_id,
            repository=repository,
            branch=branch,
        )

        logger.info(
            f'[Automation] Created execution {execution_id}',
            extra=log_ctx,
        )
        return record, True

    async def transition_state(
        self,
        execution_id: str,
        target_state: ExecutionState,
        error_message: str | None = None,
        conversation_id: str | None = None,
    ) -> ExecutionRecord | None:
        """Transition an execution to a new state with validation.

        Raises:
            ValueError: If the state transition is invalid.
        """
        record = await self._store.get_execution(execution_id)
        if not record:
            logger.error(f'[Automation] Execution {execution_id} not found')
            return None

        current_state = record.state
        validate_transition(current_state, target_state)

        updated = await self._store.update_state(
            execution_id=execution_id,
            state=target_state,
            error_message=error_message,
            conversation_id=conversation_id,
        )

        log_ctx = build_log_context(
            execution_id=execution_id,
            conversation_id=conversation_id,
        )
        if error_message:
            logger.error(
                f'[Automation] Execution {execution_id}: '
                f'{current_state.value} → {target_state.value}: {error_message}',
                extra={**log_ctx, 'error': error_message},
            )
        else:
            logger.info(
                f'[Automation] Execution {execution_id}: '
                f'{current_state.value} → {target_state.value}',
                extra=log_ctx,
            )

        return updated

    async def get_execution(
        self, execution_id: str
    ) -> ExecutionRecord | None:
        """Get an execution record."""
        return await self._store.get_execution(execution_id)

    async def list_executions(
        self,
        source_type: str | None = None,
        state: ExecutionState | None = None,
        jira_issue_key: str | None = None,
        github_pr_number: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionRecord]:
        """List executions with filters."""
        return await self._store.list_executions(
            source_type=source_type,
            state=state,
            jira_issue_key=jira_issue_key,
            github_pr_number=github_pr_number,
            limit=limit,
            offset=offset,
        )

    async def count_executions(
        self,
        source_type: str | None = None,
        state: ExecutionState | None = None,
        jira_issue_key: str | None = None,
        repository: str | None = None,
    ) -> int:
        """Count executions matching filters."""
        return await self._store.count_executions(
            source_type=source_type,
            state=state,
            jira_issue_key=jira_issue_key,
            repository=repository,
        )
