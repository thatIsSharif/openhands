"""SQLAlchemy models for the automation platform.

Follows the OSS pattern: Stored* classes with Base from sql_utils.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.utils.sql_utils import Base


class StoredExecution(Base):
    __tablename__ = 'executions'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default='RECEIVED'
    )
    jira_issue_key: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )
    github_pr_id: Mapped[int | None] = mapped_column(nullable=True)
    repository: Mapped[str | None] = mapped_column(String(255), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    max_iterations: Mapped[int | None] = mapped_column(nullable=True)
    max_budget: Mapped[float | None] = mapped_column(nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        onupdate=text('CURRENT_TIMESTAMP'),
        nullable=False,
    )


class StoredJiraIssue(Base):
    __tablename__ = 'jira_issues'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    issue_key: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reporter: Mapped[str | None] = mapped_column(String(255), nullable=True)
    labels: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    webhook_event_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    execution_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        onupdate=text('CURRENT_TIMESTAMP'),
        nullable=False,
    )


class StoredGitHubPullRequest(Base):
    __tablename__ = 'github_pull_requests'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default='open')
    execution_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        onupdate=text('CURRENT_TIMESTAMP'),
        nullable=False,
    )


class StoredJiraProjectRepository(Base):
    """Maps a Jira project key to a GitHub repository.

    Multiple rows can share the same jira_project_key to support
    projects that span multiple repositories. This table is used for
    administrative/reporting purposes; repository resolution for
    automation comes directly from the Jira issue payload.
    """

    __tablename__ = 'jira_project_repositories'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    jira_project_key: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    default_branch: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'main'")
    )
    custom_field_id: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment='Jira custom field ID (e.g. customfield_12345) '
                'that may contain a per-issue repository override',
    )
    label: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment='Descriptive label for the repo (e.g. backend, frontend). '
                'Passed into the agent prompt so it can determine '
                'whether changes are needed in this repo.',
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        onupdate=text('CURRENT_TIMESTAMP'),
        nullable=False,
    )
    github_webhook_secret: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )



class StoredReviewIteration(Base):
    __tablename__ = 'review_iterations'

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    iteration_number: Mapped[int] = mapped_column(Integer, nullable=False)
    review_comment_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    reviewer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comment_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repository: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text('CURRENT_TIMESTAMP'),
        nullable=False,
    )
