from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from openhands.app_server.utils.sql_utils import Base


class ReviewIteration(Base):
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
