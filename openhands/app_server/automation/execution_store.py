"""Execution store - persistence for execution records and automation entities.

Uses the OSS database session pattern (get_global_config().db_session).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.app_server.utils.logger import openhands_logger as logger

from .execution_models import (
    ExecutionRecord,
    ExecutionState,
    GitHubPullRequestRecord,
    JiraIssueRecord,
    JiraProjectRepositoryRecord,
    ReviewIterationRecord,
    SourceType,
)
from .models import (
    StoredExecution,
    StoredGitHubPullRequest,
    StoredJiraIssue,
    StoredJiraProjectRepository,
    StoredReviewIteration,
)


@dataclass
class ExecutionStore:
    """Manages execution records and related automation entities.

    Each method opens its own session. Accepts an optional async_sessionmaker
    for testability (defaults to the global config DB session).
    """

    session_maker: async_sessionmaker[AsyncSession] | None = field(default=None)

    @contextlib.asynccontextmanager
    async def _get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get an async session, from the provided maker or global config."""
        if self.session_maker:
            async with self.session_maker() as session:
                yield session
        else:
            from openhands.app_server.config import get_global_config

            config = get_global_config()
            maker = await config.db_session.get_async_session_maker()
            async with maker() as session:
                yield session

    async def create_execution(
        self,
        execution_id: str,
        source_type: SourceType | str,
        source_event_id: str | None = None,
        jira_issue_key: str | None = None,
        github_pr_id: int | None = None,
        repository: str | None = None,
        branch: str | None = None,
    ) -> ExecutionRecord:
        """Create a new execution record in RECEIVED state."""
        state = ExecutionState.RECEIVED.value
        execution = StoredExecution(
            execution_id=execution_id,
            source_type=source_type.value
            if isinstance(source_type, SourceType)
            else source_type,
            source_event_id=source_event_id,
            state=state,
            jira_issue_key=jira_issue_key,
            github_pr_id=github_pr_id,
            repository=repository,
            branch=branch,
        )
        async with self._get_session() as session:
            session.add(execution)
            await session.commit()
            await session.refresh(execution)
            logger.info(
                f'[Automation] Created execution {execution_id} '
                f'(state={state}, source={source_type})'
            )
            return self._record_from_model(execution)

    async def get_execution_by_conversation_id(
        self, conversation_id: str
    ) -> ExecutionRecord | None:
        """Get an execution record by conversation_id."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredExecution).filter(
                    StoredExecution.conversation_id == conversation_id
                )
            )
            execution = result.scalars().first()
            return self._record_from_model(execution) if execution else None

    async def update_state(
        self,
        execution_id: str,
        state: ExecutionState,
        error_message: str | None = None,
        conversation_id: str | None = None,
    ) -> ExecutionRecord | None:
        """Update execution state and optional metadata."""
        now = datetime.now(timezone.utc)

        async with self._get_session() as session:
            result = await session.execute(
                select(StoredExecution).filter(
                    StoredExecution.execution_id == execution_id
                )
            )
            execution = result.scalars().first()
            if not execution:
                logger.warning(
                    f'[Automation] Execution {execution_id} not found'
                )
                return None

            if isinstance(state, str):
                execution.state = state
            else:
                execution.state = state.value
            execution.updated_at = now

            if error_message is not None:
                execution.error_message = error_message
            if conversation_id is not None:
                execution.conversation_id = conversation_id
            if state == ExecutionState.RUNNING:
                execution.started_at = now
            if state in (
                ExecutionState.COMPLETED,
                ExecutionState.FAILED,
                ExecutionState.CANCELLED,
            ):
                execution.completed_at = now

            await session.commit()
            await session.refresh(execution)
            logger.info(
                f'[Automation] Execution {execution_id} state → '
                f'{state.value if hasattr(state, "value") else state}'
            )
            return self._record_from_model(execution)

    async def get_execution(
        self, execution_id: str
    ) -> ExecutionRecord | None:
        """Get an execution record by execution_id."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredExecution).filter(
                    StoredExecution.execution_id == execution_id
                )
            )
            execution = result.scalars().first()
            return self._record_from_model(execution) if execution else None

    async def get_execution_by_source_event(
        self, source_event_id: str
    ) -> ExecutionRecord | None:
        """Get execution by source event ID for idempotency checking."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredExecution).filter(
                    StoredExecution.source_event_id == source_event_id
                )
            )
            execution = result.scalars().first()
            return self._record_from_model(execution) if execution else None

    async def list_executions(
        self,
        source_type: str | None = None,
        state: ExecutionState | None = None,
        jira_issue_key: str | None = None,
        github_pr_number: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionRecord]:
        """List execution records with optional filters."""
        async with self._get_session() as session:
            query = select(StoredExecution)

            conditions = []
            if source_type:
                conditions.append(
                    StoredExecution.source_type == source_type
                )
            if state:
                conditions.append(StoredExecution.state == state.value)
            if jira_issue_key:
                conditions.append(
                    StoredExecution.jira_issue_key == jira_issue_key
                )
            if github_pr_number is not None:
                conditions.append(
                    StoredExecution.github_pr_id == github_pr_number
                )

            if conditions:
                query = query.filter(and_(*conditions))

            query = query.order_by(StoredExecution.created_at.desc())
            query = query.limit(limit).offset(offset)

            result = await session.execute(query)
            records = result.scalars().all()
            return [self._record_from_model(r) for r in records]

    async def count_executions(
        self,
        source_type: str | None = None,
        state: ExecutionState | None = None,
        jira_issue_key: str | None = None,
        repository: str | None = None,
    ) -> int:
        """Count execution records matching the given filters."""
        async with self._get_session() as session:
            query = select(StoredExecution)

            conditions = []
            if source_type:
                conditions.append(
                    StoredExecution.source_type == source_type
                )
            if state:
                conditions.append(StoredExecution.state == state.value)
            if jira_issue_key:
                conditions.append(
                    StoredExecution.jira_issue_key == jira_issue_key
                )
            if repository:
                conditions.append(
                    StoredExecution.repository == repository
                )

            if conditions:
                query = query.filter(and_(*conditions))

            result = await session.execute(query)
            return len(result.scalars().all())

    # --- Jira Issue operations ---

    async def upsert_jira_issue(
        self,
        issue_key: str,
        summary: str,
        description: str | None = None,
        issue_type: str | None = None,
        priority: str | None = None,
        reporter: str | None = None,
        labels: list[str] | None = None,
        webhook_event_id: str | None = None,
        execution_id: int | None = None,
    ) -> JiraIssueRecord:
        """Create or update a Jira issue record."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredJiraIssue).filter(
                    StoredJiraIssue.issue_key == issue_key
                )
            )
            issue = result.scalars().first()

            if issue:
                issue.summary = summary
                if description is not None:
                    issue.description = description
                if issue_type is not None:
                    issue.issue_type = issue_type
                if priority is not None:
                    issue.priority = priority
                if reporter is not None:
                    issue.reporter = reporter
                if labels is not None:
                    issue.labels = labels
                if webhook_event_id is not None:
                    issue.webhook_event_id = webhook_event_id
                if execution_id is not None:
                    issue.execution_id = execution_id
            else:
                issue = StoredJiraIssue(
                    issue_key=issue_key,
                    summary=summary,
                    description=description,
                    issue_type=issue_type,
                    priority=priority,
                    reporter=reporter,
                    labels=labels,
                    webhook_event_id=webhook_event_id,
                    execution_id=execution_id,
                )
                session.add(issue)

            await session.commit()
            await session.refresh(issue)
            return self._jira_record_from_model(issue)

    # --- GitHub Pull Request operations ---

    async def upsert_github_pull_request(
        self,
        pr_number: int,
        repository: str,
        owner: str,
        branch: str | None = None,
        title: str | None = None,
        state: str = 'open',
        execution_id: int | None = None,
        pr_url: str | None = None,
    ) -> GitHubPullRequestRecord:
        """Create or update a GitHub pull request record."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredGitHubPullRequest).filter(
                    and_(
                        StoredGitHubPullRequest.pr_number == pr_number,
                        StoredGitHubPullRequest.repository == repository,
                    )
                )
            )
            pr = result.scalars().first()

            if pr:
                if branch is not None:
                    pr.branch = branch
                if title is not None:
                    pr.title = title
                pr.state = state
                if execution_id is not None:
                    pr.execution_id = execution_id
                if pr_url is not None:
                    pr.pr_url = pr_url
            else:
                pr = StoredGitHubPullRequest(
                    pr_number=pr_number,
                    repository=repository,
                    owner=owner,
                    branch=branch,
                    title=title,
                    state=state,
                    execution_id=execution_id,
                    pr_url=pr_url,
                )
                session.add(pr)

            await session.commit()
            await session.refresh(pr)
            return self._github_pr_record_from_model(pr)

    # --- Jira Project Repository operations ---

    async def create_jira_project_repository(
        self,
        jira_project_key: str,
        repository: str,
        owner: str,
        default_branch: str = 'main',
        custom_field_id: str | None = None,
        github_webhook_secret: str | None = None,
    ) -> JiraProjectRepositoryRecord:
        """Create a Jira project → repository mapping.

        Multiple rows can share the same jira_project_key to support
        projects that span multiple repositories.
        """
        mapping = StoredJiraProjectRepository(
            jira_project_key=jira_project_key,
            repository=repository,
            owner=owner,
            default_branch=default_branch,
            custom_field_id=custom_field_id,
            github_webhook_secret=github_webhook_secret,
        )
        async with self._get_session() as session:
            session.add(mapping)
            await session.commit()
            await session.refresh(mapping)
            return self._project_repo_record_from_model(mapping)

    async def get_jira_project_repos_by_project_key(
        self, jira_project_key: str
    ) -> list[JiraProjectRepositoryRecord]:
        """Get all project→repository mappings for a Jira project key."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredJiraProjectRepository).filter(
                    StoredJiraProjectRepository.jira_project_key == jira_project_key
                )
            )
            mappings = result.scalars().all()
            return [
                self._project_repo_record_from_model(m) for m in mappings
            ]

    async def get_jira_project_repository_by_id(
        self, record_id: int
    ) -> JiraProjectRepositoryRecord | None:
        """Get a project→repository mapping by its record ID."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredJiraProjectRepository).filter(
                    StoredJiraProjectRepository.id == record_id
                )
            )
            mapping = result.scalars().first()
            return (
                self._project_repo_record_from_model(mapping)
                if mapping
                else None
            )

    async def list_jira_project_repositories(
        self,
    ) -> list[JiraProjectRepositoryRecord]:
        """List all Jira project → repository mappings."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredJiraProjectRepository).order_by(
                    StoredJiraProjectRepository.jira_project_key
                )
            )
            mappings = result.scalars().all()
            return [
                self._project_repo_record_from_model(m) for m in mappings
            ]

    async def delete_jira_project_repository(
        self, record_id: int
    ) -> bool:
        """Delete a project→repository mapping by its record ID. Returns True if deleted."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredJiraProjectRepository).filter(
                    StoredJiraProjectRepository.id == record_id
                )
            )
            mapping = result.scalars().first()
            if not mapping:
                return False
            await session.delete(mapping)
            await session.commit()
            return True

    # --- Review Iteration operations ---

    async def create_review_iteration(
        self,
        execution_id: int,
        iteration_number: int,
        review_comment_id: int | None = None,
        reviewer: str | None = None,
        comment_body: str | None = None,
        pr_number: int | None = None,
        repository: str | None = None,
    ) -> ReviewIterationRecord:
        """Create a review iteration record."""
        iteration = StoredReviewIteration(
            execution_id=execution_id,
            iteration_number=iteration_number,
            review_comment_id=review_comment_id,
            reviewer=reviewer,
            comment_body=comment_body,
            pr_number=pr_number,
            repository=repository,
        )
        async with self._get_session() as session:
            session.add(iteration)
            await session.commit()
            await session.refresh(iteration)
            return self._review_record_from_model(iteration)

    async def list_review_iterations(
        self,
        pr_number: int | None = None,
        repository: str | None = None,
        execution_id: int | None = None,
        limit: int = 50,
    ) -> list[ReviewIterationRecord]:
        """List review iterations with optional filters."""
        async with self._get_session() as session:
            query = select(StoredReviewIteration)
            conditions = []
            if pr_number is not None:
                conditions.append(
                    StoredReviewIteration.pr_number == pr_number
                )
            if repository:
                conditions.append(
                    StoredReviewIteration.repository == repository
                )
            if execution_id is not None:
                conditions.append(
                    StoredReviewIteration.execution_id == execution_id
                )

            if conditions:
                query = query.filter(and_(*conditions))

            query = query.order_by(StoredReviewIteration.created_at.desc())
            query = query.limit(limit)

            result = await session.execute(query)
            iterations = result.scalars().all()
            return [self._review_record_from_model(i) for i in iterations]

    # --- Private helpers ---

    @staticmethod
    def _record_from_model(
        execution: StoredExecution,
    ) -> ExecutionRecord:
        return ExecutionRecord(
            id=execution.id,
            execution_id=execution.execution_id,
            source_type=execution.source_type,
            source_event_id=execution.source_event_id,
            state=ExecutionState(execution.state),
            jira_issue_key=execution.jira_issue_key,
            github_pr_id=execution.github_pr_id,
            repository=execution.repository,
            branch=execution.branch,
            conversation_id=execution.conversation_id,
            error_message=execution.error_message,
            started_at=execution.started_at,
            completed_at=execution.completed_at,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
        )

    @staticmethod
    def _jira_record_from_model(
        issue: StoredJiraIssue,
    ) -> JiraIssueRecord:
        return JiraIssueRecord(
            id=issue.id,
            issue_key=issue.issue_key,
            summary=issue.summary,
            description=issue.description,
            issue_type=issue.issue_type,
            priority=issue.priority,
            reporter=issue.reporter,
            labels=issue.labels,
            webhook_event_id=issue.webhook_event_id,
            execution_id=issue.execution_id,
            created_at=issue.created_at,
            updated_at=issue.updated_at,
        )

    @staticmethod
    def _github_pr_record_from_model(
        pr: StoredGitHubPullRequest,
    ) -> GitHubPullRequestRecord:
        return GitHubPullRequestRecord(
            id=pr.id,
            pr_number=pr.pr_number,
            repository=pr.repository,
            owner=pr.owner,
            branch=pr.branch,
            title=pr.title,
            state=pr.state,
            execution_id=pr.execution_id,
            pr_url=pr.pr_url,
            created_at=pr.created_at,
            updated_at=pr.updated_at,
        )

    @staticmethod
    def _project_repo_record_from_model(
        mapping: StoredJiraProjectRepository,
    ) -> JiraProjectRepositoryRecord:
        return JiraProjectRepositoryRecord(
            id=mapping.id,
            jira_project_key=mapping.jira_project_key,
            repository=mapping.repository,
            owner=mapping.owner,
            default_branch=mapping.default_branch,
            custom_field_id=mapping.custom_field_id,
            created_at=mapping.created_at,
            updated_at=mapping.updated_at,
            github_webhook_secret=mapping.github_webhook_secret,
        )

    @staticmethod
    def _review_record_from_model(
        iteration: StoredReviewIteration,
    ) -> ReviewIterationRecord:
        return ReviewIterationRecord(
            id=iteration.id,
            execution_id=iteration.execution_id,
            iteration_number=iteration.iteration_number,
            review_comment_id=iteration.review_comment_id,
            reviewer=iteration.reviewer,
            comment_body=iteration.comment_body,
            pr_number=iteration.pr_number,
            repository=iteration.repository,
            created_at=iteration.created_at,
        )

    async def get_repository_mapping(
        self,
        owner: str,
        repository: str,
    ) -> JiraProjectRepositoryRecord | None:
        """Get repository mapping by owner/repository."""
        async with self._get_session() as session:
            result = await session.execute(
                select(StoredJiraProjectRepository).filter(
                    and_(
                        StoredJiraProjectRepository.owner == owner,
                        StoredJiraProjectRepository.repository == repository,
                    )
                )
            )

            mapping = result.scalars().first()

            return (
                self._project_repo_record_from_model(mapping)
                if mapping
                else None
            )
