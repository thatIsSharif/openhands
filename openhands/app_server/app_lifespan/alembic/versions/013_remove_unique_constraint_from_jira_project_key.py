"""Remove UNIQUE constraint from jira_project_key.

Migration: 013

Jira projects can span multiple repositories, so the same
jira_project_key must be allowed in multiple rows. Repository
resolution now comes from the Jira issue payload directly,
so the unique index is no longer needed for resolution logic.

The column remains indexed (non-unique) for efficient queries.
"""

from __future__ import annotations

from alembic import op

revision = '013'
down_revision = '012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the old UNIQUE index
    op.drop_index(
        'ix_jira_project_repositories_project_key',
        table_name='jira_project_repositories',
    )
    # Re-create as a plain (non-unique) index
    op.create_index(
        'ix_jira_project_repositories_project_key',
        'jira_project_repositories',
        ['jira_project_key'],
        unique=False,
    )


def downgrade() -> None:
    # Drop the non-unique index
    op.drop_index(
        'ix_jira_project_repositories_project_key',
        table_name='jira_project_repositories',
    )
    # Re-create the unique index
    op.create_index(
        'ix_jira_project_repositories_project_key',
        'jira_project_repositories',
        ['jira_project_key'],
        unique=True,
    )
