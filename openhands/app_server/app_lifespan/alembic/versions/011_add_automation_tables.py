"""Add automation platform tables.

Migration: 011
Adds tables for the Jira & GitHub webhook driven automation platform:

- executions: Canonical execution lifecycle records
- jira_issues: Jira issue metadata
- github_pull_requests: GitHub pull request tracking
- review_iterations: PR review cycle tracking
- jira_project_repositories: Jira project → GitHub repository mapping
"""

from __future__ import annotations

from typing import ClassVar

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '011'
down_revision: str | None = '010'
branch_labels: str | None = None
depends_on: str | None = None


def _create_executions_table() -> None:
    op.create_table(
        'executions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            'execution_id',
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            'source_type',
            sa.String(length=50),
            nullable=False,
        ),
        sa.Column(
            'source_event_id',
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            'state',
            sa.String(length=20),
            nullable=False,
            server_default='RECEIVED',
        ),
        sa.Column(
            'jira_issue_key',
            sa.String(length=50),
            nullable=True,
        ),
        sa.Column('github_pr_id', sa.Integer(), nullable=True),
        sa.Column(
            'repository',
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            'branch',
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            'conversation_id',
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_executions_execution_id',
        'executions',
        ['execution_id'],
        unique=True,
    )
    op.create_index(
        'ix_executions_source_event_id',
        'executions',
        ['source_event_id'],
        unique=True,
        postgresql_concurrently=True,
    )
    op.create_index(
        'ix_executions_jira_issue_key',
        'executions',
        ['jira_issue_key'],
        postgresql_concurrently=True,
    )
    op.create_index(
        'ix_executions_conversation_id',
        'executions',
        ['conversation_id'],
        postgresql_concurrently=True,
    )


def _create_jira_issues_table() -> None:
    op.create_table(
        'jira_issues',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            'issue_key',
            sa.String(length=50),
            nullable=False,
        ),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('issue_type', sa.String(length=50), nullable=True),
        sa.Column('priority', sa.String(length=50), nullable=True),
        sa.Column('reporter', sa.String(length=255), nullable=True),
        sa.Column(
            'labels',
            postgresql.ARRAY(sa.String()),
            nullable=True,
        ),
        sa.Column(
            'webhook_event_id',
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column('execution_id', sa.Integer(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_jira_issues_issue_key',
        'jira_issues',
        ['issue_key'],
        unique=True,
    )
    op.create_index(
        'ix_jira_issues_webhook_event_id',
        'jira_issues',
        ['webhook_event_id'],
        unique=True,
        postgresql_concurrently=True,
    )


def _create_github_pull_requests_table() -> None:
    op.create_table(
        'github_pull_requests',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('repository', sa.String(length=255), nullable=False),
        sa.Column('owner', sa.String(length=255), nullable=False),
        sa.Column('branch', sa.String(length=255), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column(
            'state',
            sa.String(length=20),
            nullable=False,
            server_default='open',
        ),
        sa.Column('execution_id', sa.Integer(), nullable=True),
        sa.Column('pr_url', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )


def _create_review_iterations_table() -> None:
    op.create_table(
        'review_iterations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('execution_id', sa.Integer(), nullable=False),
        sa.Column('iteration_number', sa.Integer(), nullable=False),
        sa.Column('review_comment_id', sa.BigInteger(), nullable=True),
        sa.Column('reviewer', sa.String(length=255), nullable=True),
        sa.Column('comment_body', sa.Text(), nullable=True),
        sa.Column('pr_number', sa.Integer(), nullable=True),
        sa.Column('repository', sa.String(length=255), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_review_iterations_execution_id',
        'review_iterations',
        ['execution_id'],
        postgresql_concurrently=True,
    )


def _create_jira_project_repositories_table() -> None:
    op.create_table(
        'jira_project_repositories',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            'jira_project_key',
            sa.String(length=50),
            nullable=False,
        ),
        sa.Column(
            'repository',
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column('owner', sa.String(length=255), nullable=False),
        sa.Column(
            'default_branch',
            sa.String(length=50),
            nullable=False,
            server_default='main',
        ),
        sa.Column(
            'custom_field_id',
            sa.String(length=50),
            nullable=True,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_jira_project_repositories_project_key',
        'jira_project_repositories',
        ['jira_project_key'],
        unique=True,
    )


def upgrade() -> None:
    _create_executions_table()
    _create_jira_issues_table()
    _create_github_pull_requests_table()
    _create_review_iterations_table()
    _create_jira_project_repositories_table()


def downgrade() -> None:
    op.drop_table('jira_project_repositories')
    op.drop_table('review_iterations')
    op.drop_table('github_pull_requests')
    op.drop_table('jira_issues')
    op.drop_table('executions')
