"""Execution models and enums for the automation platform."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ExecutionState(str, Enum):
    """Canonical execution states for automation runs."""

    RECEIVED = 'RECEIVED'
    QUEUED = 'QUEUED'
    RUNNING = 'RUNNING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'


class SourceType(str, Enum):
    """Sources that can trigger automation executions."""

    JIRA = 'jira'
    GITHUB = 'github'
    TEAMS = 'teams'


VALID_TRANSITIONS: dict[ExecutionState, set[ExecutionState]] = {
    ExecutionState.RECEIVED: {ExecutionState.QUEUED, ExecutionState.CANCELLED},
    ExecutionState.QUEUED: {ExecutionState.RUNNING, ExecutionState.CANCELLED, ExecutionState.FAILED},
    ExecutionState.RUNNING: {
        ExecutionState.COMPLETED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
    },
    ExecutionState.COMPLETED: set(),
    ExecutionState.FAILED: set(),
    ExecutionState.CANCELLED: set(),
}


@dataclass
class ExecutionRecord:
    """Represents a persisted execution record."""

    id: int | None = None
    execution_id: str = ''
    source_type: str = ''
    source_event_id: str | None = None
    state: ExecutionState = ExecutionState.RECEIVED
    jira_issue_key: str | None = None
    github_pr_id: int | None = None
    repository: str | None = None
    branch: str | None = None
    conversation_id: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class JiraIssueRecord:
    """Represents a Jira issue processed by the automation platform."""

    id: int | None = None
    issue_key: str = ''
    summary: str = ''
    description: str | None = None
    issue_type: str | None = None
    priority: str | None = None
    reporter: str | None = None
    labels: list[str] | None = None
    webhook_event_id: str | None = None
    execution_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class GitHubPullRequestRecord:
    """Represents a GitHub pull request tracked by the automation platform."""

    id: int | None = None
    pr_number: int = 0
    repository: str = ''
    owner: str = ''
    branch: str | None = None
    title: str | None = None
    state: str = 'open'
    execution_id: int | None = None
    pr_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ReviewIterationRecord:
    """Represents a review iteration triggered by a PR review comment."""

    id: int | None = None
    execution_id: int = 0
    iteration_number: int = 0
    review_comment_id: int | None = None
    reviewer: str | None = None
    comment_body: str | None = None
    pr_number: int | None = None
    repository: str | None = None
    created_at: datetime | None = None


@dataclass
class JiraProjectRepositoryRecord:
    """Maps a Jira project key to a GitHub repository."""

    id: int | None = None
    jira_project_key: str = ''
    repository: str = ''
    owner: str = ''
    default_branch: str = 'main'
    custom_field_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    github_webhook_secret: str | None = None


def validate_transition(current: ExecutionState, target: ExecutionState) -> None:
    """Validate that a state transition is legal.

    Raises:
        ValueError: If the transition is not allowed.
    """
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ValueError(
            f'Invalid state transition: {current.value} \u2192 {target.value}. '
            f'Allowed transitions from {current.value}: '
            f'{", ".join(s.value for s in allowed) or "none"}'
        )
