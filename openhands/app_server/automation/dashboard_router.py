"""Dashboard router — query automation runs from conversation_metadata.

Endpoint: GET /api/v1/automations/runs
          GET /api/v1/automations/runs/{conversation_id}
"""

from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException, Query

from openhands.agent_server.models import OpenHandsModel
from openhands.app_server.app_conversation.sql_app_conversation_info_service import (
    StoredConversationMetadata,
)
from openhands.app_server.automation.execution_store import ExecutionStore

router = APIRouter(prefix='/automations', tags=['automation'])


class AutomationRunItem(OpenHandsModel):
    """Single automation run, as returned in the paginated list."""

    conversation_id: str
    title: str | None = None
    trigger: str | None = None
    selected_repository: str | None = None
    selected_branch: str | None = None
    jira_issue_key: str | None = None
    github_pr: list[str] = []
    pr_number: list[int] = []
    llm_model: str | None = None

    # Metrics
    accumulated_cost: float | None = 0.0
    prompt_tokens: int | None = 0
    completion_tokens: int | None = 0
    total_tokens: int | None = 0
    cache_read_tokens: int | None = 0
    cache_write_tokens: int | None = 0
    reasoning_tokens: int | None = 0
    context_window: int | None = 0
    per_turn_token: int | None = 0
    max_budget_per_task: float | None = None

    # Timing
    created_at: str | None = None
    last_updated_at: str | None = None

    # Error / status
    error_message: str | None = None


class AutomationRunListResponse(OpenHandsModel):
    """Paginated list of automation runs."""

    items: list[AutomationRunItem]
    total: int
    page: int
    per_page: int
    total_pages: int


def _row_to_item(row: StoredConversationMetadata) -> AutomationRunItem:
    """Convert a StoredConversationMetadata row to an AutomationRunItem."""
    return AutomationRunItem(
        conversation_id=str(row.conversation_id),
        title=row.title,
        trigger=row.trigger,
        selected_repository=row.selected_repository,
        selected_branch=row.selected_branch,
        jira_issue_key=row.jira_issue_key,
        github_pr=row.github_pr or [],
        pr_number=row.pr_number or [],
        llm_model=row.llm_model,
        accumulated_cost=row.accumulated_cost,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_write_tokens=row.cache_write_tokens,
        reasoning_tokens=row.reasoning_tokens,
        context_window=row.context_window,
        per_turn_token=row.per_turn_token,
        max_budget_per_task=row.max_budget_per_task,
        created_at=(
            row.created_at.isoformat() if row.created_at else None
        ),
        last_updated_at=(
            row.last_updated_at.isoformat() if row.last_updated_at else None
        ),
    )


@router.get('/runs')
async def list_automation_runs(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    source: str | None = Query(
        None, description='Filter by source (jira, github)'
    ),
    search: str | None = Query(
        None, description='Search in title and jira_issue_key'
    ),
) -> AutomationRunListResponse:
    """Return paginated automation runs from conversation_metadata.

    Filters to records where trigger = 'automation'.
    Optionally filter by jira_issue_key presence (source=jira)
    or pr_number presence (source=github).
    """
    store = ExecutionStore()

    async with store._get_session() as session:
        from sqlalchemy import select

        query = select(StoredConversationMetadata).where(
            StoredConversationMetadata.trigger == 'automation'
        )

        # Source filter
        if source == 'jira':
            query = query.where(
                StoredConversationMetadata.jira_issue_key.isnot(None)
            )
        elif source == 'github':
            # GitHub automations have PR numbers but no Jira key
            from sqlalchemy import cast, String, type_coerce

            query = query.where(
                StoredConversationMetadata.jira_issue_key.is_(None)
            )

        # Search filter
        if search:
            from sqlalchemy import or_

            like_pattern = f'%{search}%'
            query = query.where(
                or_(
                    StoredConversationMetadata.title.ilike(like_pattern),
                    StoredConversationMetadata.jira_issue_key.ilike(
                        like_pattern
                    ),
                    StoredConversationMetadata.selected_repository.ilike(
                        like_pattern
                    ),
                )
            )

        # Count total
        from sqlalchemy import func as sa_func

        count_query = select(sa_func.count()).select_from(query.subquery())
        total_result = await session.execute(count_query)
        total = total_result.scalar() or 0

        # Paginate
        offset = (page - 1) * per_page
        query = (
            query.order_by(
                StoredConversationMetadata.created_at.desc()
            )
            .offset(offset)
            .limit(per_page)
        )

        result = await session.execute(query)
        rows = result.scalars().all()

        items = [_row_to_item(row) for row in rows]
        total_pages = max(1, math.ceil(total / per_page))

        return AutomationRunListResponse(
            items=items,
            total=total,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
        )


@router.get('/runs/{conversation_id}')
async def get_automation_run(
    conversation_id: str,
) -> AutomationRunItem:
    """Return a single automation run by conversation_id."""
    store = ExecutionStore()

    async with store._get_session() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(StoredConversationMetadata).where(
                StoredConversationMetadata.conversation_id == conversation_id
            )
        )
        row = result.scalars().first()

        if not row:
            raise HTTPException(
                status_code=404,
                detail=f'Automation run {conversation_id} not found',
            )

        return _row_to_item(row)
