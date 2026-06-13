"""Admin CRUD router for managing Jira project → repository mappings.

Endpoints:
- POST /api/v1/admin/jira-project-repos — Create or update a mapping
- GET /api/v1/admin/jira-project-repos — List all mappings
- GET /api/v1/admin/jira-project-repos/{project_key} — Get a single mapping
- DELETE /api/v1/admin/jira-project-repos/{project_key} — Delete a mapping
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from openhands.agent_server.models import OpenHandsModel

router = APIRouter(prefix='/admin', tags=['admin'])


class UpsertProjectRepoRequest(OpenHandsModel):
    """Request model for creating/updating a Jira project→repository mapping."""

    jira_project_key: str
    repository: str
    owner: str
    default_branch: str = 'main'
    custom_field_id: str | None = None


class ProjectRepoResponse(OpenHandsModel):
    """Response model for a Jira project→repository mapping."""

    id: int | None = None
    jira_project_key: str = ''
    repository: str = ''
    owner: str = ''
    default_branch: str = 'main'
    custom_field_id: str | None = None


class ProjectRepoListResponse(OpenHandsModel):
    """Response model for listing Jira project→repository mappings."""

    items: list[ProjectRepoResponse]


class DeleteResponse(OpenHandsModel):
    """Response model for delete operations."""

    deleted: bool


@router.post('/jira-project-repos', status_code=201)
async def upsert_project_repo(
    request: UpsertProjectRepoRequest,
) -> ProjectRepoResponse:
    """Create or update a Jira project → repository mapping."""
    from openhands.app_server.automation.execution_store import ExecutionStore

    store = ExecutionStore()
    record = await store.upsert_jira_project_repository(
        jira_project_key=request.jira_project_key,
        repository=request.repository,
        owner=request.owner,
        default_branch=request.default_branch,
        custom_field_id=request.custom_field_id,
    )
    return ProjectRepoResponse(
        id=record.id,
        jira_project_key=record.jira_project_key,
        repository=record.repository,
        owner=record.owner,
        default_branch=record.default_branch,
        custom_field_id=record.custom_field_id,
    )


@router.get('/jira-project-repos')
async def list_project_repos() -> ProjectRepoListResponse:
    """List all Jira project → repository mappings."""
    from openhands.app_server.automation.execution_store import ExecutionStore

    store = ExecutionStore()
    records = await store.list_jira_project_repositories()
    return ProjectRepoListResponse(
        items=[
            ProjectRepoResponse(
                id=r.id,
                jira_project_key=r.jira_project_key,
                repository=r.repository,
                owner=r.owner,
                default_branch=r.default_branch,
                custom_field_id=r.custom_field_id,
            )
            for r in records
        ]
    )


@router.get('/jira-project-repos/{project_key}')
async def get_project_repo(
    project_key: str,
) -> ProjectRepoResponse:
    """Get a single Jira project → repository mapping."""
    from openhands.app_server.automation.execution_store import ExecutionStore

    store = ExecutionStore()
    record = await store.get_jira_project_repository(project_key)
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f'No mapping found for Jira project "{project_key}"',
        )
    return ProjectRepoResponse(
        id=record.id,
        jira_project_key=record.jira_project_key,
        repository=record.repository,
        owner=record.owner,
        default_branch=record.default_branch,
        custom_field_id=record.custom_field_id,
    )


@router.delete('/jira-project-repos/{project_key}')
async def delete_project_repo(
    project_key: str,
) -> DeleteResponse:
    """Delete a Jira project → repository mapping."""
    from openhands.app_server.automation.execution_store import ExecutionStore

    store = ExecutionStore()
    deleted = await store.delete_jira_project_repository(project_key)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f'No mapping found for Jira project "{project_key}"',
        )
    return DeleteResponse(deleted=True)
