"""Teams webhook router — Power Automate ↔ OpenHands integration.

Endpoints called by Power Automate to initiate agent tasks from Teams
and poll execution status.

Flow 1 — Teams → OpenHands Task:
    POST /api/v1/teams/start-task
    {
        "jira_issue_key": "KAN-123"
    }

Flow 2 — PR Fix from Teams Approval:
    POST /api/v1/teams/start-pr-fix
    {
        "repository": "owner/repo",
        "pr_number": 123,
        "review_comment": "Fix the null pointer"
    }

Status polling:
    GET /api/v1/teams/status/{execution_id}
"""

from __future__ import annotations

import hashlib
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from openhands.agent_server.models import OpenHandsModel
from openhands.app_server.automation.execution_models import (
    ExecutionState,
    SourceType,
)
from openhands.app_server.automation.execution_service import (
    ExecutionService,
)
from openhands.app_server.automation.execution_store import ExecutionStore
from openhands.app_server.automation.openhands_client import (
    OpenHandsClient,
)
from openhands.app_server.automation.prompt_renderer import render_prompt
from openhands.app_server.utils.jira import (
    fetch_issue,
    get_issue_repository,
)
from openhands.app_server.utils.logger import openhands_logger as logger

router = APIRouter(prefix='/teams', tags=['automation'])

TEAMS_KNOWN_BRANCHES = frozenset({'main', 'master', 'develop'})


# ── Request / Response models ────────────────────────────────────────────


class StartTaskRequest(BaseModel):
    """Request to start an agent task from a Teams message referencing a Jira issue."""

    jira_issue_key: str
    repository: str | None = None
    custom_field_id: str | None = None


class StartPrFixRequest(BaseModel):
    """Request to start an agent PR fix from Teams approval."""

    repository: str
    pr_number: int
    review_comment: str


class TeamsStartResponse(OpenHandsModel):
    """Response to a Teams-initiated task start."""

    status: str
    execution_id: str | None = None
    conversation_id: str | None = None
    jira_issue_key: str | None = None
    repository: str | None = None
    pr_number: int | None = None
    error: str | None = None


class TeamsStatusResponse(OpenHandsModel):
    """Status response for polling execution state."""

    execution_id: str
    state: str
    jira_issue_key: str | None = None
    repository: str | None = None
    pr_number: int | None = None
    conversation_id: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


# ── Optional API key guard ───────────────────────────────────────────────


def _verify_teams_api_key(request: Request) -> None:
    """Check the Teams webhook API key if configured."""
    expected = os.environ.get('TEAMS_WEBHOOK_API_KEY', '')
    if not expected:
        return  # No key configured — allow all requests

    provided = request.headers.get('X-Teams-API-Key', '')
    if not provided:
        raise HTTPException(status_code=401, detail='Missing X-Teams-API-Key header')

    # Constant-time comparison
    if len(provided) != len(expected):
        raise HTTPException(status_code=403, detail='Invalid API key')
    result = 0
    for a, b in zip(provided.encode(), expected.encode(), strict=True):
        result |= a ^ b
    if result != 0:
        raise HTTPException(status_code=403, detail='Invalid API key')


# ── Helpers ──────────────────────────────────────────────────────────────


def _compute_teams_event_id(namespace: str, unique_ref: str) -> str:
    """Compute a deterministic event ID for idempotency."""
    raw = f'teams:{namespace}:{unique_ref}'
    return hashlib.sha256(raw.encode()).hexdigest()


async def _build_services():
    """Build the OSS service instances."""
    store = ExecutionStore()
    execution_service = ExecutionService(store=store)
    openhands_client = OpenHandsClient()
    return store, execution_service, openhands_client


# ── Flow 1: Teams → Jira Issue → Agent → PR ─────────────────────────────


@router.post('/start-task')
async def start_task(
    req: StartTaskRequest,
    request: Request,
) -> TeamsStartResponse:
    """Start an agent task from a Teams message referencing a Jira issue.

    Power Automate calls this when a user types ``@openhands work on KAN-123``
    in Teams. The agent fetches the Jira story, implements it, and creates a PR.

    Idempotency is ensured by the ``jira_issue_key`` — duplicate requests for
    the same issue return the existing execution.
    """
    _verify_teams_api_key(request)
    issue_key = req.jira_issue_key
    logger.info(f'[Teams] Starting task for Jira issue {issue_key}')

    # Fetch issue details from Jira
    try:
        issue_data = fetch_issue(issue_key)
    except (ValueError, RuntimeError) as e:
        logger.error(f'[Teams] Failed to fetch Jira issue {issue_key}: {e}')
        return TeamsStartResponse(
            status='failed',
            error=f'Failed to fetch Jira issue {issue_key}: {e}',
        )

    # Resolve repository
    repository = req.repository or get_issue_repository(
        issue_key, custom_field_id=req.custom_field_id
    )
    if not repository:
        return TeamsStartResponse(
            status='failed',
            jira_issue_key=issue_key,
            error=(
                'Could not determine repository for Jira issue '
                f'{issue_key}. Pass a ``repository`` field in the request '
                'or set the repository custom field on the Jira issue.'
            ),
        )

    # Generate a branch name
    issue_type = issue_data.get('issue_type', '')
    summary = issue_data.get('summary', '')
    type_lower = (issue_type or '').lower()
    prefix = 'bugfix' if 'bug' in type_lower else 'feature'
    slug = (
        summary.lower()
        .replace(' ', '-')
        .replace('_', '-')
    )
    # Keep only safe chars and limit length
    safe_slug = ''.join(c for c in slug if c.isalnum() or c in '-')
    safe_slug = safe_slug.strip('-')[:50].rstrip('-')
    branch = f'{prefix}/{issue_key}-{safe_slug}' if safe_slug else f'{prefix}/{issue_key}'

    # Idempotency by jira_issue_key
    event_id = _compute_teams_event_id('start-task', issue_key)

    store, execution_service, openhands_client = await _build_services()

    execution_record, is_new = await execution_service.create_execution(
        source_type=SourceType.TEAMS,
        source_event_id=event_id,
        jira_issue_key=issue_key,
        repository=repository,
        branch=branch,
    )

    execution_id = execution_record.execution_id

    if not is_new:
        return TeamsStartResponse(
            status='duplicate',
            execution_id=execution_id,
            jira_issue_key=issue_key,
            repository=repository,
        )

    # Transition to QUEUED
    await execution_service.transition_state(
        execution_id, ExecutionState.QUEUED
    )

    # Build endpoints for the agent to call when work is complete
    base_url = str(request.base_url).rstrip('/')
    comment_endpoint = f'{base_url}/api/v1/jira/start/comment'
    teams_notify_endpoint = f'{base_url}/api/v1/teams/notify'

    # Determine default branch
    default_branch = 'main'
    if '/' in repository:
        default_branch = 'main'

    # Render the Jira prompt template.
    # The agent will post a comment to Jira AND notify Teams when done
    # (the template conditionally shows the Teams step when teams_notify_endpoint is set).
    prompt = render_prompt(
        'jira_new_conversation.j2',
        issue_key=issue_key,
        title=issue_data.get('summary', ''),
        issue_type=issue_type,
        priority=issue_data.get('priority', ''),
        reporter=issue_data.get('reporter', ''),
        description=issue_data.get('description', ''),
        repository=repository,
        default_branch=default_branch,
        branch=branch,
        comment_endpoint=comment_endpoint,
        teams_notify_endpoint=teams_notify_endpoint,
    )

    # Create the agent conversation
    conversation_id = await openhands_client.create_conversation(
        state=request.state,
        request=request,
        prompt=prompt,
        title=f'[Automation] Teams — Jira {issue_key}',
        execution_id=execution_id,
        jira_issue_key=issue_key,
        repository=repository,
        branch=branch,
    )

    if conversation_id:
        await execution_service.transition_state(
            execution_id,
            ExecutionState.RUNNING,
            conversation_id=conversation_id,
        )
        return TeamsStartResponse(
            status='running',
            execution_id=execution_id,
            conversation_id=conversation_id,
            jira_issue_key=issue_key,
            repository=repository,
        )
    else:
        await execution_service.transition_state(
            execution_id,
            ExecutionState.FAILED,
            error_message='Failed to create OpenHands conversation',
        )
        return TeamsStartResponse(
            status='failed',
            execution_id=execution_id,
            jira_issue_key=issue_key,
            error='Failed to create OpenHands conversation',
        )


# ── Flow 2: Teams Approval → PR Fix ─────────────────────────────────────


@router.post('/start-pr-fix')
async def start_pr_fix(
    req: StartPrFixRequest,
    request: Request,
) -> TeamsStartResponse:
    """Start an agent PR fix from Teams approval.

    Power Automate calls this when a user clicks "Fix issues?" on an
    adaptive card in Teams. The agent reads the PR context, applies fixes,
    and pushes to the existing branch.
    """
    _verify_teams_api_key(request)
    logger.info(
        f'[Teams] Starting PR fix for {req.repository} PR #{req.pr_number}'
    )

    # Idempotency by repo + pr_number
    event_id = _compute_teams_event_id(
        'pr-fix', f'{req.repository}:{req.pr_number}'
    )

    store, execution_service, openhands_client = await _build_services()

    execution_record, is_new = await execution_service.create_execution(
        source_type=SourceType.TEAMS,
        source_event_id=event_id,
        github_pr_id=req.pr_number,
        repository=req.repository,
    )

    execution_id = execution_record.execution_id

    if not is_new:
        return TeamsStartResponse(
            status='duplicate',
            execution_id=execution_id,
            repository=req.repository,
            pr_number=req.pr_number,
        )

    # Transition to QUEUED
    await execution_service.transition_state(
        execution_id, ExecutionState.QUEUED
    )

    # Build endpoints for the agent to call when work is complete
    base_url = str(request.base_url).rstrip('/')
    comment_endpoint = f'{base_url}/api/v1/git/github/webhook/comment'
    teams_notify_endpoint = f'{base_url}/api/v1/teams/notify'

    # Render the GitHub review prompt template.
    # The agent will post a PR comment AND notify Teams when done
    # (the template conditionally shows the Teams step when teams_notify_endpoint is set).
    prompt = render_prompt(
        'github_review_conversation.j2',
        pr_number=req.pr_number,
        repository=req.repository,
        reviewer='Teams User',
        review_comment=req.review_comment,
        branch='',
        comment_endpoint=comment_endpoint,
        teams_notify_endpoint=teams_notify_endpoint,
    )

    # Create the agent conversation
    conversation_id = await openhands_client.create_conversation(
        state=request.state,
        request=request,
        prompt=prompt,
        title=f'[Automation] Teams — PR #{req.pr_number} Fix',
        execution_id=execution_id,
        pr_number=req.pr_number,
        repository=req.repository,
    )

    if conversation_id:
        await execution_service.transition_state(
            execution_id,
            ExecutionState.RUNNING,
            conversation_id=conversation_id,
        )
        return TeamsStartResponse(
            status='running',
            execution_id=execution_id,
            conversation_id=conversation_id,
            repository=req.repository,
            pr_number=req.pr_number,
        )
    else:
        await execution_service.transition_state(
            execution_id,
            ExecutionState.FAILED,
            error_message='Failed to create OpenHands conversation',
        )
        return TeamsStartResponse(
            status='failed',
            execution_id=execution_id,
            repository=req.repository,
            pr_number=req.pr_number,
            error='Failed to create OpenHands conversation',
        )


# ── Notify endpoint (called by the agent when work completes) ────────────


class TeamsNotifyRequest(BaseModel):
    """Request from the agent to notify Teams that work is complete."""

    jira_issue_key: str | None = None
    repository: str | None = None
    pr_number: int | None = None
    message: str = ''


@router.post('/notify')
async def notify_teams(req: TeamsNotifyRequest) -> dict:
    """Notify Teams that an agent task is complete.

    The agent calls this endpoint (via a POST request) when it finishes
    implementing a Jira issue or fixing a PR, following the same pattern
    used by the Jira ``/jira/start/comment`` and GitHub
    ``/git/github/webhook/comment`` endpoints.

    This endpoint forwards the notification to Power Automate via
    ``TEAMS_NOTIFICATION_WEBHOOK_URL``.
    """
    webhook_url = os.environ.get('TEAMS_NOTIFICATION_WEBHOOK_URL', '')
    if not webhook_url:
        logger.warning(
            '[Teams] notify called but TEAMS_NOTIFICATION_WEBHOOK_URL '
            'is not set — notification not sent'
        )
        return {'status': 'skipped', 'reason': 'TEAMS_NOTIFICATION_WEBHOOK_URL not configured'}

    payload = {
        'jira_issue_key': req.jira_issue_key,
        'repository': req.repository,
        'pr_number': req.pr_number,
        'message': req.message,
        'state': 'COMPLETED',
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
            )
            logger.info(
                f'[Teams] Notification sent (HTTP {resp.status_code})'
            )
            return {'status': 'ok', 'http_status': resp.status_code}
    except Exception as e:
        logger.error(f'[Teams] Failed to send notification: {e}')
        return {'status': 'failed', 'error': str(e)}


# ── Status Polling ───────────────────────────────────────────────────────


@router.get('/status/{execution_id}')
async def get_execution_status(execution_id: str) -> TeamsStatusResponse:
    """Poll the status of a Teams-initiated execution.

    Power Automate calls this periodically until the state reaches a
    terminal value (``COMPLETED``, ``FAILED``, ``CANCELLED``).
    """
    store = ExecutionStore()
    record = await store.get_execution(execution_id)

    if not record:
        raise HTTPException(
            status_code=404,
            detail=f'Execution {execution_id} not found',
        )

    return TeamsStatusResponse(
        execution_id=record.execution_id,
        state=record.state.value,
        jira_issue_key=record.jira_issue_key,
        repository=record.repository,
        pr_number=record.github_pr_id,
        conversation_id=record.conversation_id,
        error_message=record.error_message,
        created_at=str(record.created_at) if record.created_at else None,
        started_at=str(record.started_at) if record.started_at else None,
        completed_at=str(record.completed_at) if record.completed_at else None,
    )
