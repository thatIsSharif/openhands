"""Add rate limit columns to executions table.

Migration: 015
Adds max_iterations and max_budget columns to the executions table
for per-task rate limit enforcement in automation conversations.

- max_iterations: Optional max iterations override for the task.
- max_budget: Optional max budget override (in USD) for the task.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "max_iterations",
            sa.Integer(),
            nullable=True,
            comment=(
                "Optional max iterations override for the task. "
                "When NULL, the user's global max_iterations is used."
            ),
        ),
    )
    op.add_column(
        "executions",
        sa.Column(
            "max_budget",
            sa.Float(),
            nullable=True,
            comment=(
                "Optional max budget override (in USD) for the task. "
                "When NULL, no budget enforcement is applied."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("executions", "max_budget")
    op.drop_column("executions", "max_iterations")
