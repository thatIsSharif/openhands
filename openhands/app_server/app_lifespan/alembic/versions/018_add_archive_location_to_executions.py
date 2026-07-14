"""Add archive_location column to executions table.

Migration: 018
Adds archive_location TEXT column to the executions table for
storing the S3 key where an archived execution's sandbox state
is persisted.

Revision ID: 018
Revises: 017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '018'
down_revision = '017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Guard: if create_all() ran first on a fresh DB the column may
    # already exist.  Check before adding to avoid a duplicate-column
    # error on SQLite (which does not support IF NOT EXISTS in ALTER).
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('executions')]
    if 'archive_location' not in columns:
        op.add_column(
            'executions',
            sa.Column(
                'archive_location',
                sa.Text(),
                nullable=True,
                comment='S3 key where the archived execution state is persisted',
            ),
        )


def downgrade() -> None:
    op.drop_column('executions', 'archive_location')
