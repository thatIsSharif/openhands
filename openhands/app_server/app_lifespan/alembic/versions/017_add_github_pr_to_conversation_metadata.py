"""Add github_pr column to conversation_metadata table.

Migration: 017
Adds github_pr JSON column to the conversation_metadata table for
storing GitHub PR URLs associated with a Jira issue conversation.

Revision ID: 017
Revises: 016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '017'
down_revision = '016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'conversation_metadata',
        sa.Column(
            'github_pr',
            sa.JSON(),
            nullable=True,
            comment=(
                'List of GitHub PR URLs created for the associated '
                'Jira issue'
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column('conversation_metadata', 'github_pr')
