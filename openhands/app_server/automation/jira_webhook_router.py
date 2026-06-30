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

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from openhands.agent_server.models import OpenHandsModel
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.user.specifiy_user_context import ADMIN, USER_CONTEXT_ATTR
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
from openhands.app_server.sandbox.sandbox_models import SandboxStatus


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

    # Resume the sandbox if needed
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
        if sandbox.status == SandboxStatus.PAUSED:
            logger.info(
                f'[Automation] Resuming sandbox {sandbox_id} for '
                f'{issue_key}'
            )
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
                return False

            # Refresh sandbox info after resume
            sandbox = await sandbox_service.get_sandbox(sandbox_id)

        elif sandbox.status == SandboxStatus.MISSING:
            logger.warning(
                f'[Automation] Sandbox {sandbox_id} for {issue_key} '
                'is missing, cannot resume'
            )
            return False

    # Send the comment as a message to the conversation
    async with get_httpx_client(
        request.state, request
    ) as httpx_client:
        try:
            # Get fresh sandbox info for the agent server URL and session key
            async with get_sandbox_service(
                request.state, request
            ) as sandbox_service:
                sandbox = await sandbox_service.get_sandbox(sandbox_id)

            if not sandbox or not sandbox.exposed_urls:
                logger.warning(
                    f'[Automation] Cannot send message for {issue_key}: '
                    'sandbox has no exposed URLs'
                )
                return True

            agent_server_url = None
            for exposed_url in sandbox.exposed_urls:
                if exposed_url.name == AGENT_SERVER:
                    agent_server_url = exposed_url.url
                    break

            if not agent_server_url:
                logger.warning(
                    f'[Automation] Cannot send message for {issue_key}: '
                    'no agent server URL found'
                )
                return True

            agent_server_url = replace_localhost_hostname_for_docker(
                agent_server_url
            )

            # Build the token-usage endpoint from the request
            base_url = str(request.base_url).rstrip('/')
            token_usage_url = (
                f'{base_url}/api/v1/jira/start/token-usage'
            )

            # Build the message: include the comment body, issue reference,
            # and instruction to post token usage when done
            message_text = (
                f'**[Jira comment on {issue_key}]**\n\n'
                f'{comment_body}\n\n'
                f'---\n'
                f'When you finish addressing this, post token usage metrics '
                f'by sending a POST request to:\n'
                f'POST {token_usage_url}\n'
                f'{{"issue_key": "{issue_key}", "body": ""}}\n'
                f'(Uses X-Session-API-Key auth. Creates or updates a single '
                f'token-usage comment per issue.)'
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
                        'X-Session-API-Key': (
                            sandbox.session_api_key
                        )
                    }
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
            import traceback
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
        else:
            logger.info(
                f'[Automation] Jira event processed: {result.get("status")} '
                f'(execution: {result.get("execution_id", "N/A")})',
            )

    except Exception:
        import traceback

        logger.error(traceback.format_exc())

@router.post('/comment')
async def post_jira_comment(req: JiraCommentRequest) -> dict:
    """Post a comment to Jira. LLM calls this — code handles the Jira API."""

    result = add_comment(req.issue_key, req.body)
    return {'status': 'ok', 'comment_id': result['id']}


@router.post('/token-usage')
async def post_jira_token_usage(
    req: JiraCommentRequest,
    request: Request,
) -> dict:
    """Post or update a token-usage comment on a Jira issue.

    Called by the agent at the end of a JIRA task. Uses the sandbox's
    session API key to look up the conversation and fetch LIVE metrics
    from the agent server (not the DB, which may have stale data).
    Creates or updates a single token-usage comment per issue.
    """
    from openhands.app_server.config import (
        get_app_conversation_info_service,
        get_httpx_client,
    )
    from openhands.app_server.sandbox.session_auth import (
        validate_session_key,
    )
    from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER
    from openhands.app_server.utils.docker_utils import (
        replace_localhost_hostname_for_docker,
    )
    from openhands.app_server.utils.jira import (
        add_or_update_token_usage_comment,
    )

    # Validate the session API key to identify the sandbox
    session_api_key = request.headers.get('X-Session-API-Key', '')
    sandbox = await validate_session_key(session_api_key)

    # Find the agent server URL from sandbox exposed URLs
    agent_server_url = None
    for exposed_url in (sandbox.exposed_urls or []):
        if exposed_url.name == AGENT_SERVER:
            agent_server_url = exposed_url.url
            break

    if not agent_server_url:
        logger.warning(
            '[Automation] No agent server URL found for sandbox '
            f'{sandbox.id}'
        )
        return {'status': 'error', 'message': 'No agent server URL'}

    agent_server_url = replace_localhost_hostname_for_docker(
        agent_server_url
    )

    # Find the latest conversation ID for this sandbox
    state = InjectorState()
    setattr(state, USER_CONTEXT_ATTR, ADMIN)
    async with get_app_conversation_info_service(
        state
    ) as info_service:
        page = await info_service.search_app_conversation_info(
            sandbox_id__eq=sandbox.id,
            limit=1,
        )

    conv_info = page.items[0] if page.items else None
    if not conv_info:
        logger.warning(
            f'[Automation] No conversation found for sandbox {sandbox.id}'
        )
        return {'status': 'error', 'message': 'No conversation found'}

    conversation_id = conv_info.id

    # Fetch LIVE conversation data from the agent server
    # (DB metrics may be stale/zero since they haven't been flushed yet)
    accumulated_cost = 0.0
    model_name = 'default'
    prompt_tokens = 0
    completion_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    reasoning_tokens = 0

    async with get_httpx_client(state) as httpx_client:
        try:
            live_url = (
                f'{agent_server_url}/api/conversations/{conversation_id}'
            )
            live_resp = await httpx_client.get(
                live_url,
                headers={'X-Session-API-Key': session_api_key},
                timeout=10.0,
            )
            if live_resp.status_code == 200:
                live_data = live_resp.json()
                # Navigate to metrics from stats.usage_to_metrics.agent
                stats = live_data.get('stats') or {}
                usage_to_metrics = stats.get('usage_to_metrics') or {}
                agent_metrics = usage_to_metrics.get('agent') or {}
                accumulated_cost = agent_metrics.get(
                    'accumulated_cost', 0.0
                )
                model_name = agent_metrics.get(
                    'model_name', 'default'
                )

                usage = (
                    agent_metrics.get('accumulated_token_usage') or {}
                )
                prompt_tokens = usage.get('prompt_tokens', 0)
                completion_tokens = usage.get('completion_tokens', 0)
                cache_read_tokens = usage.get('cache_read_tokens', 0)
                cache_write_tokens = usage.get('cache_write_tokens', 0)
                reasoning_tokens = usage.get('reasoning_tokens', 0)

                logger.info(
                    f'[Automation] Fetched live metrics for '
                    f'{conversation_id}: cost={accumulated_cost}, '
                    f'tokens={prompt_tokens + completion_tokens}'
                )
            else:
                logger.warning(
                    f'[Automation] Failed to fetch live conversation '
                    f'{conversation_id}: HTTP {live_resp.status_code}'
                )
                # Fall back to DB metrics
                if conv_info.metrics:
                    metrics = conv_info.metrics
                    accumulated_cost = metrics.accumulated_cost
                    model_name = metrics.model_name
                    if metrics.accumulated_token_usage:
                        tu = metrics.accumulated_token_usage
                        prompt_tokens = tu.prompt_tokens
                        completion_tokens = tu.completion_tokens
                        cache_read_tokens = tu.cache_read_tokens
                        cache_write_tokens = tu.cache_write_tokens
                        reasoning_tokens = tu.reasoning_tokens
        except Exception:
            import traceback
            logger.error(
                f'[Automation] Error fetching live conversation '
                f'{conversation_id}: {traceback.format_exc()}'
            )
            # Fall back to DB metrics
            if conv_info.metrics:
                metrics = conv_info.metrics
                accumulated_cost = metrics.accumulated_cost
                model_name = metrics.model_name
                if metrics.accumulated_token_usage:
                    tu = metrics.accumulated_token_usage
                    prompt_tokens = tu.prompt_tokens
                    completion_tokens = tu.completion_tokens
                    cache_read_tokens = tu.cache_read_tokens
                    cache_write_tokens = tu.cache_write_tokens
                    reasoning_tokens = tu.reasoning_tokens

    # Build the comment body with token usage details
    lines = [
        '*OpenHands Automation Complete*',
        '',
        f'*Total Cost:* ${accumulated_cost:.6f}',
        f'*Model:* {model_name}',
    ]

    total_tokens = prompt_tokens + completion_tokens
    if total_tokens > 0 or prompt_tokens > 0 or completion_tokens > 0:
        lines.append('')
        lines.append('*Token Usage:*')
        lines.append(f'- Prompt tokens: {prompt_tokens:,}')
        lines.append(f'- Completion tokens: {completion_tokens:,}')
        lines.append(f'- Total tokens: {total_tokens:,}')
        if cache_read_tokens:
            lines.append(
                f'- Cache read tokens: {cache_read_tokens:,}'
            )
        if cache_write_tokens:
            lines.append(
                f'- Cache write tokens: {cache_write_tokens:,}'
            )
        if reasoning_tokens:
            lines.append(
                f'- Reasoning tokens: {reasoning_tokens:,}'
            )

    # Look up execution record for budget info
    store = ExecutionStore()
    record = await store.get_execution_by_conversation_id(
        str(conversation_id)
    )
    if record and record.max_budget and record.max_budget > 0:
        pct = accumulated_cost / record.max_budget * 100
        lines.append('')
        lines.append(
            f'*Budget Usage:* ${accumulated_cost:.4f}'
            f' / ${record.max_budget:.4f} ({pct:.1f}%)'
        )

    comment_body = '\n'.join(lines)
    result = add_or_update_token_usage_comment(req.issue_key, comment_body)
    logger.info(
        f'[Automation] Token usage comment posted/updated on {req.issue_key}'
    )
    return {'status': 'ok', 'comment_id': result.get('id', '')}
