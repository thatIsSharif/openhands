"""Add jira_issue_key column to conversation_metadata table.

Migration: 016
Adds jira_issue_key column to the conversation_metadata table for
cross-referencing conversations with Jira issues. This enables
looking up existing conversations when @openhands mentions arrive
via Jira comment webhooks.

Revision ID: 016
Revises: 015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '016'
down_revision = '015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'conversation_metadata',
        sa.Column(
            'jira_issue_key',
            sa.String(),
            nullable=True,
            comment=(
                'Jira issue key for cross-referencing conversations '
                'with Jira issues'
            ),
        ),
    )
    op.create_index(
        'ix_conversation_metadata_jira_issue_key',
        'conversation_metadata',
        ['jira_issue_key'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        'ix_conversation_metadata_jira_issue_key',
        table_name='conversation_metadata',
    )
    op.drop_column('conversation_metadata', 'jira_issue_key')
