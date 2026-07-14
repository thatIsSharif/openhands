"""REST API endpoints for workspace export and restore."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from openhands.app_server.app_conversation.app_conversation_info_service import (
    AppConversationInfoService,
)
from openhands.app_server.app_conversation.app_conversation_service import (
    AppConversationService,
)
from openhands.app_server.config import (
    depends_app_conversation_info_service,
    depends_app_conversation_service,
    depends_sandbox_service,
    get_workspace_export_service,
    get_workspace_restore_service,
)
from openhands.app_server.sandbox.sandbox_service import SandboxService
from openhands.app_server.services.injector import InjectorState

_logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/v1', tags=['Workspace Export'])

# FastAPI dependency instances: called at module level so the decorator
# sees Depends() instances as default values, not raw function objects.
_app_conversation_service_dep = depends_app_conversation_service()
_app_conversation_info_service_dep = depends_app_conversation_info_service()
_sandbox_service_dep = depends_sandbox_service()


@router.post('/workspace-exports/{conversation_id}')
async def export_conversation(
    conversation_id: UUID,
    request: Request,
    app_conversation_service: AppConversationService = _app_conversation_service_dep,
    app_conversation_info_service: AppConversationInfoService = _app_conversation_info_service_dep,
    sandbox_service: SandboxService = _sandbox_service_dep,
) -> dict:
    """Manually trigger a workspace export for a conversation."""
    state = InjectorState()
    info = await app_conversation_info_service.get_app_conversation_info(
        conversation_id
    )
    if not info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Conversation {conversation_id} not found',
        )

    jira_key = info.jira_issue_key
    if not jira_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Conversation has no jira_issue_key — cannot export',
        )

    async with get_workspace_export_service(state, request) as export_service:
        result = await export_service.export_conversation(
            conversation_id=conversation_id,
            jira_key=jira_key,
            app_conversation_service=app_conversation_service,
            app_conversation_info_service=app_conversation_info_service,
            docker_sandbox_service=sandbox_service,
        )

    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.error_message or 'Export failed',
        )

    return {
        'success': True,
        'jira_key': jira_key,
        'snapshot_tag': result.snapshot_tag,
        'exported_at': result.exported_at,
    }


@router.post('/workspace-restores/{jira_key}')
async def restore_conversation(
    jira_key: str,
    new_sandbox_id: str | None = None,
    request: Request | None = None,
    sandbox_service: SandboxService = _sandbox_service_dep,
) -> dict:
    """Restore a workspace from a previously exported snapshot."""
    state = InjectorState()

    async with get_workspace_restore_service(state, request) as restore_service:
        result = await restore_service.restore_conversation(
            jira_key=jira_key,
            new_sandbox_id=new_sandbox_id or f'restored-{jira_key.lower()}',
            docker_sandbox_service=sandbox_service,
        )

    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND
            if 'no snapshot' in (result.error_message or '').lower()
            else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.error_message or 'Restore failed',
        )

    return {
        'success': True,
        'jira_key': jira_key,
        'sandbox_id': result.sandbox_info.id if result.sandbox_info else None,
    }


@router.get('/workspace-exports/{jira_key}')
async def check_export(
    jira_key: str,
    request: Request | None = None,
) -> dict:
    """Check if an export exists for a given Jira key."""
    state = InjectorState()
    async with get_workspace_export_service(state, request) as export_service:
        exists = await export_service._storage.exists(jira_key)
    return {'exists': exists, 'jira_key': jira_key}


@router.delete('/workspace-exports/{jira_key}')
async def delete_export(
    jira_key: str,
    request: Request | None = None,
) -> dict:
    """Delete a stored export for a given Jira key."""
    state = InjectorState()
    async with get_workspace_export_service(state, request) as export_service:
        deleted = await export_service._storage.delete(jira_key)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'No export found for {jira_key}',
        )
    return {'success': True, 'jira_key': jira_key}
