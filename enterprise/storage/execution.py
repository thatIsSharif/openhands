from datetime import datetime

from sqlalchemy import DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.utils.sql_utils import Base


class Execution(Base):
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
    github_pr_id: Mapped[int | None] = mapped_column(
        nullable=True
    )
    repository: Mapped[str | None] = mapped_column(String(255), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
