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

import os
import re
import traceback

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from openhands.agent_server.models import OpenHandsModel
from openhands.app_server.automation.execution_service import (
    ExecutionService,
)
from openhands.app_server.automation.execution_store import ExecutionStore
from openhands.app_server.automation.input_sanitizer import sanitize_input
from openhands.app_server.automation.jira_automation_service import (
    JiraAutomationService,
    verify_jira_signature,
)
from openhands.app_server.automation.openhands_client import (
    OpenHandsClient,
)
from openhands.app_server.automation.prompt_renderer import render_prompt
from openhands.app_server.config import (
    get_app_conversation_info_service,
    get_httpx_client,
    get_sandbox_service,
)
from openhands.app_server.sandbox.sandbox_models import SandboxStatus
from openhands.app_server.sandbox.session_auth import validate_session_key
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.user.specifiy_user_context import ADMIN, USER_CONTEXT_ATTR
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)
from openhands.app_server.utils.jira import (
    _get_agent_url_from_sandbox,
    add_comment,
    add_or_update_token_usage_comment,
    build_token_usage_comment,
    fetch_live_agent_metrics,
)
from openhands.app_server.utils.logger import openhands_logger as logger
from openhands.app_server.utils.sandbox_utils import pause_sandbox


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
            return JiraWebhookResponse(status='rejected', reason='Invalid signature')

    # Schedule background processing
    background_tasks.add_task(_process_jira_event, payload, request)

    return JiraWebhookResponse(
        status='accepted',
        issue_key=(payload.get('issue', {}).get('key')),
    )


async def _handle_comment_created(
    payload: dict,
    request: Request,
) -> bool:
    """Handle a comment_created webhook event with @openhands mention.

    If the comment contains @openhands, looks up an existing conversation
    for the issue. If found, resumes the sandbox and forwards the comment
    as a message. If not found, creates a new conversation for the issue.

    Returns:
        True if the comment was handled (conversation found and resumed,
        or new conversation created), False if the comment does not
        mention @openhands.
    """
    issue = payload.get('issue', {})
    issue_key = issue.get('key')
    comment = payload.get('comment', {})
    comment_body = (comment.get('body', '') or '').strip()

    if not issue_key or not comment_body:
        logger.info('[Automation] Comment event missing issue_key or comment body')
        return False

    # Check for @openhands mention (case-insensitive)
    if '@openhands' not in comment_body.lower():
        logger.info(
            f'[Automation] Comment on {issue_key} does not mention @openhands, skipping'
        )
        return False

    logger.info(f'[Automation] @openhands mentioned in comment on {issue_key}')

    # Look up existing conversation by Jira issue key
    async with get_app_conversation_info_service(
        request.state, request
    ) as info_service:
        conversation = await info_service.get_conversation_by_jira_issue_key(issue_key)

    if not conversation:
        logger.info(
            f'[Automation] No existing conversation found for {issue_key}, '
            'creating a new conversation'
        )
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
            request=request,
        )

        logger.info(
            f'[Automation] New conversation created for {issue_key} '
            f'via @openhands comment: {result.get("status")} '
            f'(execution: {result.get("execution_id", "N/A")})'
        )
        return True

    conversation_id = conversation.id
    sandbox_id = conversation.sandbox_id
    logger.info(
        f'[Automation] Found conversation {conversation_id} for '
        f'{issue_key} (sandbox: {sandbox_id})'
    )

    # Resume the sandbox if needed
    async with get_sandbox_service(request.state, request) as sandbox_service:
        sandbox = await sandbox_service.get_sandbox(sandbox_id)
        if sandbox is None:
            logger.warning(
                f'[Automation] Sandbox {sandbox_id} for conversation '
                f'{conversation_id} not found, creating new conversation'
            )
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
                request=request,
            )

            logger.info(
                f'[Automation] New conversation created for {issue_key} '
                f'via @openhands comment (sandbox missing): '
                f'{result.get("status")} '
                f'(execution: {result.get("execution_id", "N/A")})'
            )
            return True
        if sandbox.status == SandboxStatus.PAUSED:
            logger.info(f'[Automation] Resuming sandbox {sandbox_id} for {issue_key}')
            await sandbox_service.resume_sandbox(sandbox_id)

            # Wait for sandbox to be fully ready
            try:
                await sandbox_service.wait_for_sandbox_running(
                    sandbox_id,
                    timeout=60,
                    poll_interval=2,
                )
            except TimeoutError:
                logger.error(
                    f'[Automation] Sandbox {sandbox_id} did not become ready '
                    f'after 60 seconds'
                )
                return True

            # Refresh sandbox info after resume
            sandbox = await sandbox_service.get_sandbox(sandbox_id)

        elif sandbox.status == SandboxStatus.MISSING:
            logger.warning(
                f'[Automation] Sandbox {sandbox_id} for {issue_key} '
                'is missing, cannot resume, creating new conversation'
            )
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
                request=request,
            )

            logger.info(
                f'[Automation] New conversation created for {issue_key} '
                f'via @openhands comment (sandbox missing): '
                f'{result.get("status")} '
                f'(execution: {result.get("execution_id", "N/A")})'
            )
            return True

    # Send the comment as a message to the conversation
    async with get_httpx_client(request.state, request) as httpx_client:
        try:
            # Get fresh sandbox info for the agent server URL and session key
            async with get_sandbox_service(request.state, request) as sandbox_service:
                sandbox = await sandbox_service.get_sandbox(sandbox_id)

            if not sandbox or not sandbox.exposed_urls:
                logger.warning(
                    f'[Automation] Cannot send message for {issue_key}: '
                    'sandbox has no exposed URLs'
                )
                return True

            agent_server_url = _get_agent_url_from_sandbox(sandbox)
            if not agent_server_url:
                logger.warning(
                    f'[Automation] Cannot send message for {issue_key}: '
                    'no agent server URL found'
                )
                return True

            agent_server_url = replace_localhost_hostname_for_docker(agent_server_url)

            # Build the token-usage endpoint from the request
            base_url = str(request.base_url).rstrip('/')
            token_usage_url = f'{base_url}/api/v1/jira/start/token-usage'

            # ── Input sanitization (Layer 1) ────────────────────────
            safe_comment_body = sanitize_input(
                comment_body, field_name='jira_existing_comment'
            )

            # Render the message from the existing-conversation template
            message_text = render_prompt(
                'jira_existing_conversation.j2',
                issue_key=issue_key,
                comment_body=safe_comment_body,
                token_usage_url=token_usage_url,
            )

            response = await httpx_client.post(
                f'{agent_server_url}/api/conversations/{conversation_id}/events',
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
                    {'X-Session-API-Key': (sandbox.session_api_key)}
                    if sandbox.session_api_key
                    else {}
                ),
                timeout=60.0,
            )
            response.raise_for_status()

            logger.info(
                f'[Automation] Comment from {issue_key} forwarded to '
                f'conversation {conversation_id}'
            )

        except Exception as e:
            logger.error(
                f'[Automation] Failed to send comment for {issue_key} '
                f'to conversation {conversation_id}: {type(e).__name__}: {e}\n'
            )

    return True


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

        # Read target account ID from environment (set JIRA_TARGET_ACCOUNT_ID)
        target_account_id = os.environ.get('JIRA_TARGET_ACCOUNT_ID', '')

        # Only process assignment events
        if payload.get('issue_event_type_name') != 'issue_assigned':
            logger.info('[Automation] Ignoring Jira event: not an assignment event')
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
            logger.info('[Automation] Ignoring Jira event: no assignee change found')
            return

        if target_account_id and assignee_change.get('to') != target_account_id:
            logger.info(
                '[Automation] Ignoring Jira event: not assigned to target user '
                f'(to={assignee_change.get("to")})'
            )
            return
        elif not target_account_id:
            logger.info(
                '[Automation] No JIRA_TARGET_ACCOUNT_ID set, processing any assignee'
            )

        logger.info('[Automation] Issue assigned to target user, starting automation')

        # Build services using OSS DI
        store = ExecutionStore()
        execution_service = ExecutionService(store=store)
        openhands_client = OpenHandsClient()

        jira_service = JiraAutomationService(
            execution_service=execution_service,
            openhands_client=openhands_client,
        )

        result = await jira_service.process_issue_created(
            payload=payload, state=request.state, request=request
        )

        if result.get('status') == 'multi':
            executions = result.get('executions', [])
            ids = ', '.join(e.get('execution_id', 'N/A') for e in executions)
            logger.info(
                f'[Automation] Jira event processed: multi '
                f'({len(executions)} executions: {ids})',
            )
        else:
            logger.info(
                f'[Automation] Jira event processed: {result.get("status")} '
                f'(execution: {result.get("execution_id", "N/A")})',
            )

    except Exception:
        logger.error(traceback.format_exc())


@router.post('/comment')
async def post_jira_comment(
    req: JiraCommentRequest,
    request: Request,
) -> dict:
    """Post a comment to Jira and update conversation metadata with PR links.

    After posting the comment, extracts any GitHub PR URLs from the comment
    body and stores them in the conversation metadata for cross-referencing.
    LLM calls this — code handles the Jira API and metadata updates.
    """
    result = add_comment(req.issue_key, req.body)
    comment_id = result.get('id', '')

    # Extract GitHub PR URLs from the comment body
    pr_urls = re.findall(
        r'https://github\.com/[\w.-]+/[\w.-]+/pull/\d+',
        req.body,
    )

    if pr_urls:
        try:
            state = InjectorState()
            setattr(state, USER_CONTEXT_ATTR, ADMIN)
            async with get_app_conversation_info_service(
                state, request
            ) as info_service:
                conversation = await info_service.get_conversation_by_jira_issue_key(
                    req.issue_key
                )
                if conversation:
                    # Merge with existing PRs, deduplicating while preserving order
                    seen = set(conversation.github_pr)
                    new_prs = [url for url in pr_urls if url not in seen]
                    if new_prs:
                        conversation.github_pr = conversation.github_pr + new_prs
                        await info_service.save_app_conversation_info(conversation)
                        logger.info(
                            '[Automation] Updated conversation %s github_pr: '
                            'added %d new PR(s) (total %d) for Jira issue %s',
                            conversation.id,
                            len(new_prs),
                            len(conversation.github_pr),
                            req.issue_key,
                        )

        except Exception:
            logger.error(
                '[Automation] Failed to update github_pr metadata '
                'for Jira issue %s: %s',
                req.issue_key,
                traceback.format_exc(),
            )

    return {'status': 'ok', 'comment_id': comment_id}


@router.post('/token-usage')
async def post_jira_token_usage(
    req: JiraCommentRequest,
    request: Request,
) -> dict:
    """Post/update a beautified token-usage comment on a Jira issue.

    Called by the agent at task completion. Fetches LIVE metrics from
    the agent server (not the DB) and posts or updates a single comment
    per issue (identified by the 🎯 *OpenHands Automation Complete* marker).
    """
    # Validate session and find sandbox
    session_api_key = request.headers.get('X-Session-API-Key', '')
    sandbox = await validate_session_key(session_api_key)

    agent_server_url = _get_agent_url_from_sandbox(sandbox)
    if not agent_server_url:
        return {'status': 'error', 'message': 'No agent server URL'}

    # Find the conversation ID for this sandbox
    state = InjectorState()
    setattr(state, USER_CONTEXT_ATTR, ADMIN)
    async with get_app_conversation_info_service(state) as info_service:
        page = await info_service.search_app_conversation_info(
            sandbox_id__eq=sandbox.id,
            limit=1,
        )

    conv_info = page.items[0] if page.items else None
    if not conv_info:
        return {'status': 'error', 'message': 'No conversation found'}

    conversation_id = str(conv_info.id)

    # Fetch live metrics from agent server, fall back to DB
    async with get_httpx_client(state) as httpx_client:
        live = await fetch_live_agent_metrics(
            agent_server_url,
            conversation_id,
            session_api_key,
            httpx_client,
        )

    if not live and conv_info.metrics:
        m = conv_info.metrics
        live = {
            'accumulated_cost': m.accumulated_cost,
            'model_name': m.model_name,
            'prompt_tokens': (
                m.accumulated_token_usage.prompt_tokens
                if m.accumulated_token_usage
                else 0
            ),
            'completion_tokens': (
                m.accumulated_token_usage.completion_tokens
                if m.accumulated_token_usage
                else 0
            ),
            'cache_read_tokens': (
                m.accumulated_token_usage.cache_read_tokens
                if m.accumulated_token_usage
                else 0
            ),
            'cache_write_tokens': (
                m.accumulated_token_usage.cache_write_tokens
                if m.accumulated_token_usage
                else 0
            ),
            'reasoning_tokens': (
                m.accumulated_token_usage.reasoning_tokens
                if m.accumulated_token_usage
                else 0
            ),
        }

    # Look up execution record for budget info
    store = ExecutionStore()
    record = await store.get_execution_by_conversation_id(conversation_id)
    max_budget = (
        record.max_budget
        if record and record.max_budget and record.max_budget > 0
        else None
    )

    comment_body = build_token_usage_comment(
        accumulated_cost=live.get('accumulated_cost', 0.0),
        model_name=live.get('model_name', 'default'),
        prompt_tokens=live.get('prompt_tokens', 0),
        completion_tokens=live.get('completion_tokens', 0),
        cache_read_tokens=live.get('cache_read_tokens', 0),
        cache_write_tokens=live.get('cache_write_tokens', 0),
        reasoning_tokens=live.get('reasoning_tokens', 0),
        max_budget=max_budget,
        created_at=live.get('created_at'),
        updated_at=live.get('updated_at'),
    )

    result = add_or_update_token_usage_comment(req.issue_key, comment_body)
    logger.info(f'[Automation] Token usage comment posted/updated on {req.issue_key}')

    # Pause sandbox after task completion
    if sandbox and sandbox.id:
        try:
            await pause_sandbox(sandbox.id, state, request)
        except Exception:
            logger.error(
                '[Automation] Failed to pause sandbox for Jira issue %s: %s',
                req.issue_key,
                traceback.format_exc(),
            )

    return {'status': 'ok', 'comment_id': result.get('id', '')}
