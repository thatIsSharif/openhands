"""Add automation platform tables (executions, jira_issues, github_pull_requests, review_iterations)

This migration creates the core tables for the Jira & GitHub Webhook Driven
OpenHands Automation Platform (KAN-17). These tables support:

- Execution lifecycle tracking (RECEIVED → QUEUED → RUNNING → COMPLETED/FAILED)
- Jira issue metadata storage and correlation
- GitHub pull request tracking
- Review iteration history for PR review comment workflows
- Idempotency enforcement via unique source_event_id indexes

Revision ID: 120
Revises: 119
Create Date: 2026-06-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '120'
down_revision: Union[str, None] = '119'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- executions ---
    op.create_table(
        'executions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('execution_id', sa.String(255), nullable=False),
        sa.Column('source_type', sa.String(50), nullable=False),
        sa.Column('source_event_id', sa.String(255), nullable=True),
        sa.Column('state', sa.String(20), nullable=False, server_default='RECEIVED'),
        sa.Column('jira_issue_key', sa.String(50), nullable=True),
        sa.Column('github_pr_id', sa.Integer(), nullable=True),
        sa.Column('repository', sa.String(255), nullable=True),
        sa.Column('branch', sa.String(255), nullable=True),
        sa.Column('conversation_id', sa.String(255), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_executions_execution_id',
        'executions',
        ['execution_id'],
        unique=True,
        if_not_exists=True,
    )
    op.create_index(
        'idx_executions_source_event_id',
        'executions',
        ['source_event_id'],
        unique=True,
        postgresql_where=sa.text('source_event_id IS NOT NULL'),
        if_not_exists=True,
    )
    op.create_index(
        'idx_executions_state',
        'executions',
        ['state'],
        if_not_exists=True,
    )
    op.create_index(
        'idx_executions_jira_issue_key',
        'executions',
        ['jira_issue_key'],
        if_not_exists=True,
    )
    op.create_index(
        'idx_executions_conversation_id',
        'executions',
        ['conversation_id'],
        if_not_exists=True,
    )

    # --- jira_issues ---
    op.create_table(
        'jira_issues',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('issue_key', sa.String(50), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('issue_type', sa.String(50), nullable=True),
        sa.Column('priority', sa.String(50), nullable=True),
        sa.Column('reporter', sa.String(255), nullable=True),
        sa.Column('labels', postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column('webhook_event_id', sa.String(255), nullable=True),
        sa.Column('execution_id', sa.Integer(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('issue_key'),
        sa.UniqueConstraint('webhook_event_id'),
    )
    op.create_index(
        'idx_jira_issues_issue_key',
        'jira_issues',
        ['issue_key'],
        if_not_exists=True,
    )

    # --- github_pull_requests ---
    op.create_table(
        'github_pull_requests',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('repository', sa.String(255), nullable=False),
        sa.Column('owner', sa.String(255), nullable=False),
        sa.Column('branch', sa.String(255), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('state', sa.String(20), nullable=False, server_default='open'),
        sa.Column('execution_id', sa.Integer(), nullable=True),
        sa.Column('pr_url', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pr_number', 'repository'),
    )
    op.create_index(
        'idx_github_pr_pr_number',
        'github_pull_requests',
        ['pr_number'],
        if_not_exists=True,
    )
    op.create_index(
        'idx_github_pr_repository',
        'github_pull_requests',
        ['repository'],
        if_not_exists=True,
    )

    # --- review_iterations ---
    op.create_table(
        'review_iterations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('execution_id', sa.Integer(), nullable=False),
        sa.Column('iteration_number', sa.Integer(), nullable=False),
        sa.Column('review_comment_id', sa.BigInteger(), nullable=True),
        sa.Column('reviewer', sa.String(255), nullable=True),
        sa.Column('comment_body', sa.Text(), nullable=True),
        sa.Column('pr_number', sa.Integer(), nullable=True),
        sa.Column('repository', sa.String(255), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(),
            server_default=sa.text('CURRENT_TIMESTAMP'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_review_iterations_execution_id',
        'review_iterations',
        ['execution_id'],
        if_not_exists=True,
    )
    op.create_index(
        'idx_review_iterations_pr',
        'review_iterations',
        ['pr_number', 'repository'],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table('review_iterations')
    op.drop_table('github_pull_requests')
    op.drop_table('jira_issues')
    op.drop_table('executions')
