"""Jira webhook router - handles incoming Jira webhook events.

Endpoint: POST /api/v1/webhooks/jira

Flow:
1. Validate webhook signature
2. Parse event type
3. Process asynchronously via BackgroundTasks
4. Return 202 accepted immediately

Supports:
- Issue assignment events (jira:issue_updated with assignee change)
- Comment mention events (comment_created with @openhands)
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from openhands.agent_server.models import OpenHandsModel
from openhands.app_server.automation.execution_service import (
    ExecutionService,
)
from openhands.app_server.automation.execution_store import ExecutionStore
from openhands.app_server.automation.jira_automation_service import (
    JiraAutomationService,
    verify_jira_signature,
)
from openhands.app_server.automation.openhands_client import (
    OpenHandsClient,
)
from openhands.app_server.config import (
    get_app_conversation_info_service,
    get_httpx_client,
    get_sandbox_service,
)
from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)
from openhands.app_server.utils.jira import add_comment
from openhands.app_server.utils.logger import openhands_logger as logger


class JiraCommentRequest(BaseModel):
    issue_key: str
    body: str

router = APIRouter(prefix='/jira/start', tags=['automation'])


class JiraWebhookResponse(OpenHandsModel):
    """Response model for Jira webhook endpoint."""

    status: str
    execution_id: str | None = None
    issue_key: str | None = None
    conversation_id: str | None = None
    reason: str | None = None
    error: str | None = None


@router.post('')
async def handle_jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> JiraWebhookResponse:
    """Handle an incoming Jira webhook event.

    Validates the signature, extracts issue data, and schedules
    background processing. Returns immediately with HTTP 202.
    """
    body = await request.body()
    payload = await request.json()

    webhook_event = payload.get('webhookEvent', '')
    logger.info(
        f'[Automation] Jira webhook received: {webhook_event}',
    )

    # Only process issue creation/update and comment events
    if webhook_event not in (
        'jira:issue_created',
        'jira:issue_updated',
        'comment_created',
    ):
        return JiraWebhookResponse(
            status='ignored',
            reason=f'Unsupported event: {webhook_event}',
        )

    # Verify signature (optional - requires JIRA_WEBHOOK_SECRET env var)
    jira_secret = os.environ.get('JIRA_WEBHOOK_SECRET', '')
    if jira_secret:
        signature = request.headers.get('X-Hub-Signature')
        if not verify_jira_signature(body, signature, jira_secret):
            logger.warning('[Automation] Invalid Jira webhook signature')
            return JiraWebhookResponse(
                status='rejected', reason='Invalid signature'
            )

    # Schedule background processing
    background_tasks.add_task(
        _process_jira_event, payload, request
    )

    return JiraWebhookResponse(
        status='accepted',
        issue_key=(
            payload.get('issue', {}).get('key')
        ),
    )


async def _handle_comment_created(
    payload: dict,
    request: Request,
) -> bool:
    """Handle a comment_created webhook event with @openhands mention.

    If the comment contains @openhands, looks up an existing conversation
    for the issue. If found, resumes the sandbox and forwards the comment
    as a message. If not found, returns False so the caller can fall
    through to the normal issue-creation flow.

    Returns:
        True if the comment was handled (conversation found and resumed),
        False if no matching conversation exists.
    """
    issue = payload.get('issue', {})
    issue_key = issue.get('key')
    comment = payload.get('comment', {})
    comment_body = (comment.get('body', '') or '').strip()

    if not issue_key or not comment_body:
        logger.info(
            '[Automation] Comment event missing issue_key or comment body'
        )
        return False

    # Check for @openhands mention (case-insensitive)
    if '@openhands' not in comment_body.lower():
        logger.info(
            f'[Automation] Comment on {issue_key} does not mention @openhands, '
            'skipping'
        )
        return False

    logger.info(
        f'[Automation] @openhands mentioned in comment on {issue_key}'
    )

    # Look up existing conversation by Jira issue key
    async with get_app_conversation_info_service(
        request.state, request
    ) as info_service:
        conversation = await info_service.get_conversation_by_jira_issue_key(
            issue_key
        )

    if not conversation:
        logger.info(
            f'[Automation] No existing conversation found for {issue_key}, '
            'falling through to new conversation creation'
        )
        return False

    conversation_id = conversation.id
    sandbox_id = conversation.sandbox_id
    logger.info(
        f'[Automation] Found conversation {conversation_id} for '
        f'{issue_key} (sandbox: {sandbox_id})'
    )

    # Resume the sandbox if needed and capture agent_server_url
    # from the sandbox info *before* resuming. Paused containers now
    # return exposed_urls (port mappings are available even when paused),
    # so we can construct the URL without needing to re-fetch after resume.
    agent_server_url = None
    session_api_key = None

    async with get_sandbox_service(
        request.state, request
    ) as sandbox_service:
        sandbox = await sandbox_service.get_sandbox(sandbox_id)
        if sandbox is None:
            logger.warning(
                f'[Automation] Sandbox {sandbox_id} for conversation '
                f'{conversation_id} not found'
            )
            return False

        # Capture agent server URL from sandbox exposed URLs
        if sandbox.exposed_urls:
            for exposed_url in sandbox.exposed_urls:
                if exposed_url.name == AGENT_SERVER:
                    agent_server_url = exposed_url.url
                    break

        # Capture session_api_key from paused/running container
        session_api_key = sandbox.session_api_key

        if sandbox.status == 'PAUSED' or sandbox.status == 'paused':
            logger.info(
                f'[Automation] Resuming sandbox {sandbox_id} for '
                f'{issue_key}'
            )
            await sandbox_service.resume_sandbox(sandbox_id)
        elif sandbox.status == 'MISSING' or sandbox.status == 'missing':
            logger.warning(
                f'[Automation] Sandbox {sandbox_id} for {issue_key} '
                'is missing, cannot resume'
            )
            return False

    if not agent_server_url:
        logger.warning(
            f'[Automation] Cannot send message for {issue_key}: '
            'no agent server URL found'
        )
        return True

    agent_server_url = replace_localhost_hostname_for_docker(
        agent_server_url
    )

    # Send the comment as a message to the conversation
    async with get_httpx_client(
        request.state, request
    ) as httpx_client:
        try:

            # Build the message: include both the comment body and
            # the Jira issue reference
            message_text = (
                f'**[Jira comment on {issue_key}]**\n\n'
                f'{comment_body}'
            )

            response = await httpx_client.post(
                f'{agent_server_url}/api/conversations/'
                f'{conversation_id}/events',
                json={
                    'role': 'user',
                    'content': [
                        {
                            'type': 'text',
                            'text': message_text,
                        }
                    ],
                    'run': True,
                },
                headers=(
                    {
                        'X-Session-API-Key': session_api_key,
                    }
                    if session_api_key
                    else {}
                ),
                timeout=30.0,
            )
            response.raise_for_status()

            logger.info(
                f'[Automation] Comment from {issue_key} forwarded to '
                f'conversation {conversation_id}'
            )

            # Spawn a background task to wait for the agent to finish
            # processing and then pause the sandbox. This ensures the
            # sandbox is paused even if the event callback processor
            # path has any issues delivering the terminal event.
            asyncio.ensure_future(
                _monitor_and_pause_sandbox(
                    agent_server_url=agent_server_url,
                    conversation_id=str(conversation_id),
                    session_api_key=(
                        sandbox.session_api_key if sandbox.session_api_key else ''
                    ),
                    sandbox_id=sandbox_id,
                )
            )

        except Exception as e:
            logger.error(
                f'[Automation] Failed to send comment for {issue_key} '
                f'to conversation {conversation_id}: {e}'
            )

    return True


async def _monitor_and_pause_sandbox(
    agent_server_url: str,
    conversation_id: str,
    session_api_key: str,
    sandbox_id: str,
) -> None:
    """Poll conversation status and pause the sandbox when agent finishes.

    Runs as a background task after forwarding a Jira comment. Polls the
    agent server's conversation endpoint for execution_status and pauses
    the sandbox once a terminal state (finished/error/stuck) is reached.
    Times out after 5 minutes.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for _ in range(300):
                try:
                    resp = await client.get(
                        f'{agent_server_url}/api/conversations/{conversation_id}',
                        headers={
                            'X-Session-API-Key': session_api_key,
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        exec_status = data.get('execution_status', '')
                        if exec_status in ('finished', 'error', 'stuck'):
                            await _pause_sandbox_by_id(sandbox_id)
                            logger.info(
                                '[Automation] Paused sandbox %s after '
                                'agent finished (status=%s)',
                                sandbox_id,
                                exec_status,
                            )
                            return
                except Exception:
                    pass
                await asyncio.sleep(1)

            logger.warning(
                '[Automation] Timed out waiting for conversation %s '
                'to finish, sandbox %s may still be running',
                conversation_id,
                sandbox_id,
            )
    except Exception:
        logger.exception(
            '[Automation] Error monitoring sandbox %s', sandbox_id
        )


async def _pause_sandbox_by_id(sandbox_id: str) -> None:
    """Pause a sandbox by ID, using the injector pattern."""
    from openhands.app_server.config import get_sandbox_service
    from openhands.app_server.services.injector import InjectorState
    from openhands.app_server.user.specifiy_user_context import (
        ADMIN,
        USER_CONTEXT_ATTR,
    )

    state = InjectorState()
    setattr(state, USER_CONTEXT_ATTR, ADMIN)
    async with get_sandbox_service(state) as sandbox_service:
        await sandbox_service.pause_sandbox(sandbox_id)


async def _monitor_execution_and_pause(
    execution_id: str,
    issue_key: str,
) -> None:
    """Poll execution state and pause sandbox when execution completes.

    Runs as a background task after creating a conversation for a Jira
    assignment event. Polls the execution store for a terminal state
    (COMPLETED/FAILED/CANCELLED), then pauses the associated sandbox.
    Times out after 30 minutes.
    """
    from openhands.app_server.automation.execution_models import ExecutionState
    from openhands.app_server.automation.execution_store import ExecutionStore
    from openhands.app_server.config import (
        get_app_conversation_service,
        get_sandbox_service,
    )
    from openhands.app_server.services.injector import InjectorState
    from openhands.app_server.user.specifiy_user_context import (
        ADMIN,
        USER_CONTEXT_ATTR,
    )

    store = ExecutionStore()
    try:
        for _ in range(1800):
            record = await store.get_execution(execution_id)
            if not record:
                logger.info(
                    '[Automation] Execution %s for %s no longer exists',
                    execution_id,
                    issue_key,
                )
                return

            if record.state in (
                ExecutionState.COMPLETED,
                ExecutionState.FAILED,
                ExecutionState.CANCELLED,
            ):
                conversation_id = record.conversation_id
                if not conversation_id:
                    logger.info(
                        '[Automation] No conversation_id for '
                        'execution %s (%s)',
                        execution_id,
                        issue_key,
                    )
                    return

                # Look up the app conversation to get sandbox_id
                injector_state = InjectorState()
                setattr(injector_state, USER_CONTEXT_ATTR, ADMIN)
                async with (
                    get_app_conversation_service(injector_state)
                    as app_conversation_service,
                    get_sandbox_service(injector_state) as sandbox_service,
                ):
                    app_conversation = (
                        await app_conversation_service.get_app_conversation(
                            conversation_id
                        )
                    )
                    if not app_conversation:
                        logger.warning(
                            '[Automation] AppConversation %s not found '
                            'for execution %s (%s)',
                            conversation_id,
                            execution_id,
                            issue_key,
                        )
                        return

                    sandbox_id = app_conversation.sandbox_id
                    if sandbox_id:
                        await sandbox_service.pause_sandbox(sandbox_id)
                        logger.info(
                            '[Automation] Paused sandbox %s after '
                            'execution %s completed (%s)',
                            sandbox_id,
                            execution_id,
                            issue_key,
                        )
                return

            await asyncio.sleep(1)

        logger.warning(
            '[Automation] Timed out monitoring execution %s for %s',
            execution_id,
            issue_key,
        )
    except Exception:
        logger.exception(
            '[Automation] Error monitoring execution %s for %s',
            execution_id,
            issue_key,
        )


async def _process_jira_event(
    payload: dict,
    request: Request,
) -> None:
    """Process a Jira webhook event in the background."""

    try:
        webhook_event = payload.get('webhookEvent', '')

        # Handle comment_created events (with @openhands mention)
        if webhook_event == 'comment_created':
            handled = await _handle_comment_created(payload, request)
            if handled:
                logger.info(
                    f'[Automation] Comment event on '
                    f'{payload.get("issue", {}).get("key")} handled'
                )
                return
            # If not handled (no existing conversation), fall through
            # to create a new conversation via the assignment flow
            logger.info(
                '[Automation] Comment not handled by existing conversation, '
                'falling through to issue assignment flow'
            )

        # Read target account ID from environment (set JIRA_TARGET_ACCOUNT_ID)
        target_account_id = os.environ.get('JIRA_TARGET_ACCOUNT_ID', '')

        # Only process assignment events
        if payload.get('issue_event_type_name') != 'issue_assigned':
            logger.info(
                '[Automation] Ignoring Jira event: not an assignment event'
            )
            return

        assignee_change = next(
            (
                item
                for item in payload.get('changelog', {}).get('items', [])
                if item.get('field') == 'assignee'
            ),
            None,
        )

        if not assignee_change:
            logger.info(
                '[Automation] Ignoring Jira event: no assignee change found'
            )
            return

        if target_account_id and assignee_change.get('to') != target_account_id:
            logger.info(
                "[Automation] Ignoring Jira event: not assigned to target user "
                f"(to={assignee_change.get('to')})"
            )
            return
        elif not target_account_id:
            logger.info(
                '[Automation] No JIRA_TARGET_ACCOUNT_ID set, processing any assignee'
            )

        logger.info(
            '[Automation] Issue assigned to target user, starting automation'
        )

        # Extract issue_key for background monitoring
        issue = payload.get('issue', {})
        issue_key = issue.get('key', 'unknown')

        # Build services using OSS DI
        store = ExecutionStore()
        execution_service = ExecutionService(store=store)
        openhands_client = OpenHandsClient()

        jira_service = JiraAutomationService(
            execution_service=execution_service,
            openhands_client=openhands_client,
        )

        result = await jira_service.process_issue_created(
            payload=payload,
            state=request.state,
            request=request
        )

        if result.get('status') == 'multi':
            executions = result.get('executions', [])
            ids = ', '.join(
                e.get('execution_id', 'N/A') for e in executions
            )
            logger.info(
                f'[Automation] Jira event processed: multi '
                f'({len(executions)} executions: {ids})',
            )

            # Start background monitor for each execution
            for execution in executions:
                execution_id = execution.get('execution_id')
                if execution_id:
                    asyncio.ensure_future(
                        _monitor_execution_and_pause(
                            execution_id=execution_id,
                            issue_key=issue_key,
                        )
                    )
        else:
            logger.info(
                f'[Automation] Jira event processed: {result.get("status")} '
                f'(execution: {result.get("execution_id", "N/A")})',
            )

            # Start background monitor if a conversation was created
            conversation_id = result.get('conversation_id')
            execution_id = result.get('execution_id')
            if conversation_id and execution_id:
                asyncio.ensure_future(
                    _monitor_execution_and_pause(
                        execution_id=execution_id,
                        issue_key=issue_key,
                    )
                )

    except Exception:
        import traceback

        logger.error(traceback.format_exc())

@router.post('/comment')
async def post_jira_comment(req: JiraCommentRequest) -> dict:
    """Post a comment to Jira. LLM calls this — code handles the Jira API."""

    result = add_comment(req.issue_key, req.body)
    return {'status': 'ok', 'comment_id': result['id']}
