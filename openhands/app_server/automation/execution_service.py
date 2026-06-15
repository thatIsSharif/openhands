"""Execution service - business logic for the execution lifecycle.

Coordinates state machine transitions, idempotency checks, and
persistence of execution records. Creates Langfuse traces when
transitions to RUNNING occur and Langfuse is configured.
"""

from __future__ import annotations

from dataclasses import dataclass

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context, generate_execution_id
from .execution_models import (
    ExecutionRecord,
    ExecutionState,
    SourceType,
    validate_transition,
)
from .execution_store import ExecutionStore


@dataclass
class ExecutionService:
    """Coordinates execution lifecycle operations.

    Delegates persistence to ExecutionStore and enforces
    state machine validity rules.
    """

    store: ExecutionStore

    async def create_execution(
        self,
        source_type: SourceType | str,
        source_event_id: str | None = None,
        jira_issue_key: str | None = None,
        github_pr_id: int | None = None,
        repository: str | None = None,
        branch: str | None = None,
    ) -> tuple[ExecutionRecord, bool]:
        """Create a new execution record with idempotency check.

        If an execution with the same source_event_id already exists,
        returns the existing record with is_new=False.

        Returns:
            Tuple of (ExecutionRecord, is_new).
        """
        # Idempotency: check for existing execution by source event ID
        if source_event_id:
            existing = await self.store.get_execution_by_source_event(
                source_event_id
            )
            if existing:
                logger.info(
                    f'[Automation] Duplicate event {source_event_id} '
                    f'already processed as execution {existing.execution_id}',
                    extra=build_log_context(
                        execution_id=existing.execution_id,
                        jira_issue_key=jira_issue_key,
                    ),
                )
                return existing, False

        execution_id = generate_execution_id()

        record = await self.store.create_execution(
            execution_id=execution_id,
            source_type=source_type,
            source_event_id=source_event_id,
            jira_issue_key=jira_issue_key,
            github_pr_id=github_pr_id,
            repository=repository,
            branch=branch,
        )

        logger.info(
            f'[Automation] Created execution {execution_id} '
            f'(source={source_type}, jira_issue={jira_issue_key})',
            extra=build_log_context(
                execution_id=execution_id,
                jira_issue_key=jira_issue_key,
                repository=repository,
            ),
        )

        return record, True

    async def transition_state(
        self,
        execution_id: str,
        target: ExecutionState,
        error_message: str | None = None,
        conversation_id: str | None = None,
    ) -> ExecutionRecord | None:
        """Transition an execution to a new state.

        Validates the transition, then persists the change.
        Creates a Langfuse trace when transitioning to RUNNING
        if Langfuse is configured.

        Returns:
            Updated ExecutionRecord, or None if not found.
        """
        record = await self.store.get_execution(execution_id)
        if not record:
            logger.warning(
                f'[Automation] Execution {execution_id} not found for transition'
            )
            return None

        current = record.state
        if isinstance(target, str):
            target = ExecutionState(target)

        validate_transition(current, target)

        updated = await self.store.update_state(
            execution_id=execution_id,
            state=target,
            error_message=error_message,
            conversation_id=conversation_id,
        )

        if updated:
            target_str = target.value if hasattr(target, "value") else target
            logger.info(
                f'[Automation] Execution {execution_id} '
                f'→ {target_str}',
                extra=build_log_context(
                    execution_id=execution_id,
                    conversation_id=conversation_id,
                ),
            )

        # Create Langfuse trace when execution starts running
        if target == ExecutionState.RUNNING and updated:
            from .langfuse_service import LangfuseService

            langfuse = LangfuseService()
            await langfuse.start_trace(updated)

        return updated
