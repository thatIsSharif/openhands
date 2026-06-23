from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jira_project_repositories",
        sa.Column(
            "label",
            sa.String(50),
            nullable=True,
            comment="Descriptive label for the repo (e.g. backend, frontend)",
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "jira_project_repositories",
        "label",
    )
