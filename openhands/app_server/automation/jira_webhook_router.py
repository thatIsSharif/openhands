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
import re
import traceback

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from openhands.agent_server.models import OpenHandsModel
from openhands.app_server.automation.execution_service import (
    ExecutionService,
)
from openhands.app_server.automation.execution_store import ExecutionStore
from openhands.app_server.automation.input_sanitizer import (
    has_dangerous_patterns,
    build_rejection_message
)
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

        # Start background polling for archive
        if result.get('status') == 'running' and result.get('conversation_id'):
            asyncio.create_task(
                _poll_execution_for_archive(request, result)
            )
        return True

    conversation_id = conversation.id
    sandbox_id = conversation.sandbox_id
    logger.info(
        f'[Automation] Found conversation {conversation_id} for '
        f'{issue_key} (sandbox: {sandbox_id})'
    )

    # Resume / restore the sandbox if needed
    async with get_sandbox_service(request.state, request) as sandbox_service:
        sandbox = await sandbox_service.get_sandbox(sandbox_id)
        if sandbox is None or sandbox.status == SandboxStatus.MISSING:
            logger.info(
                f'[Automation] Sandbox {sandbox_id} not available for '
                f'{issue_key}, attempting archive restore'
            )
            restored = await _restore_archived_conversation(
                issue_key=issue_key,
                conversation_id=conversation_id,
                payload=payload,
                request=request,
            )
            if restored:
                return True
            logger.warning(
                f'[Automation] Archive restore failed for {issue_key}, '
                'creating new conversation'
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
                f'via @openhands comment (restore failed): '
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
            is_dangerous, labels = has_dangerous_patterns(
                comment_body, field_name='jira_existing_comment'
            )
            if is_dangerous:
                logger.warning(
                    '[Security] Rejecting comment on %s (existing conversation): '
                    'dangerous patterns=%s',
                    issue_key, labels,
                )
                add_comment(issue_key, build_rejection_message(comment_body))
                return True

            # Render the message from the existing-conversation template
            message_text = render_prompt(
                'jira_existing_conversation.j2',
                issue_key=issue_key,
                comment_body=comment_body,
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
            # Start background polling for each execution
            for exec_info in executions:
                if exec_info.get('conversation_id') and exec_info.get('execution_id'):
                    asyncio.create_task(
                        _poll_execution_for_archive(
                            request, result, exec_info,
                        )
                    )
        elif result.get('status') == 'running':
            logger.info(
                f'[Automation] Jira event processed: {result.get("status")} '
                f'(execution: {result.get("execution_id", "N/A")})',
            )
            if result.get('conversation_id'):
                asyncio.create_task(
                    _poll_execution_for_archive(
                        request, result,
                    )
                )
        else:
            logger.info(
                f'[Automation] Jira event processed: {result.get("status")} '
                f'(execution: {result.get("execution_id", "N/A")})',
            )

    except Exception:
        logger.error(traceback.format_exc())


async def _poll_execution_for_archive(
    request: Request,
    result: dict,
    exec_info: dict | None = None,
) -> None:
    """Background task: poll for conversation completion then archive.

    Bypasses the event webhook pipeline entirely by polling the agent
    server's ``GET /api/conversations/{id}`` endpoint directly.  When the
    conversation reaches a terminal state, archives to S3 and destroys
    the sandbox.
    """
    from .callback_processors import AutomationEventCallbackProcessor

    conv_id = (
        exec_info.get('conversation_id')
        if exec_info
        else result.get('conversation_id')
    )
    exec_id = (
        exec_info.get('execution_id')
        if exec_info
        else result.get('execution_id')
    )
    if not conv_id or not exec_id:
        return

    await AutomationEventCallbackProcessor.poll_and_archive(
        state=request.state,
        request=request,
        conversation_id=str(conv_id),
        execution_id=str(exec_id),
        jira_issue_key=result.get('issue_key'),
        repository=exec_info.get('repository') if exec_info else result.get('repository'),
        pr_number=exec_info.get('pr_number') if exec_info else result.get('pr_number'),
    )


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

async def _restore_archived_conversation(
    *,
    issue_key: str,
    conversation_id: str,
    payload: dict,
    request: Request,
) -> bool:
    """Restore an archived conversation from S3 into a fresh sandbox.

    Returns True if the restore succeeded and the conversation resumed,
    False if anything failed (caller should fall back to new conversation).
    """
    try:
        from openhands.app_server.automation.input_sanitizer import (
            has_dangerous_patterns,
        )
        from openhands.app_server.automation.prompt_renderer import render_prompt
        from openhands.app_server.automation.sandbox_archive_service import (
            SandboxArchiveService,
        )
        from openhands.app_server.config import (
            get_app_conversation_info_service,
            get_httpx_client,
            get_sandbox_service,
        )
        from openhands.app_server.file_store.s3 import S3FileStore
        from openhands.app_server.sandbox.sandbox_models import (
            AGENT_SERVER,
        )
        from openhands.app_server.utils.docker_utils import (
            replace_localhost_hostname_for_docker,
        )

        store = ExecutionStore()
        archived = await store.get_latest_archived_execution(
            jira_issue_key=issue_key,
        )
        if not archived or not archived.archive_location:
            logger.info(
                f'[Automation] No archived execution found for {issue_key}'
            )
            return False

        logger.info(
            f'[Automation] Found archived execution {archived.execution_id} '
            f'for {issue_key} at {archived.archive_location}'
        )

        # Get conversation metadata for repo info
        async with get_app_conversation_info_service(
            request.state, request
        ) as info_service:
            conv_info = await info_service.get_app_conversation_info(
                conversation_id
            )

        repo = conv_info.selected_repository if conv_info else None
        branch = conv_info.selected_branch if conv_info else 'main'

        logger.info(
            f'[Automation] Restore step 1/6: creating sandbox (repo=%s, branch=%s)',
            repo, branch,
        )

        # Create a fresh sandbox
        async with (
            get_sandbox_service(request.state, request) as sandbox_service,
            get_httpx_client(request.state, request) as httpx_client,
        ):
            logger.info('[Automation] Restore step 2/6: start_sandbox()...')
            sandbox = await sandbox_service.start_sandbox()
            logger.info(
                '[Automation] Restore step 2/6: sandbox_id=%s, waiting...',
                sandbox.id,
            )
            sandbox = await sandbox_service.wait_for_sandbox_running(
                sandbox.id, timeout=120, poll_interval=2,
            )
            logger.info('[Automation] Restore step 2/6: sandbox running')

            # Resolve agent server URL
            logger.info('[Automation] Restore step 3/6: resolving agent URL...')
            agent_url = None
            for eu in sandbox.exposed_urls or []:
                if eu.name == AGENT_SERVER:
                    agent_url = eu.url
                    break
            if not agent_url:
                logger.error('[Automation] Restore step 3/6: NO agent URL found!')
                return False
            agent_url = replace_localhost_hostname_for_docker(agent_url)
            logger.info(
                '[Automation] Restore step 3/6: agent_url=%s', agent_url,
            )

            # Restore conversation archive
            logger.info('[Automation] Restore step 4/6: restoring archive from S3...')
            s3_store = S3FileStore()
            archive_svc = SandboxArchiveService(
                s3_store=s3_store,
                httpx_client=httpx_client,
            )

            # Clone the repo before restoring conversation (agent needs files)
            if repo:
                logger.info(
                    '[Automation] Restore step 4.5/6: cloning %s (branch=%s)...',
                    repo, branch,
                )
                parts = repo.split('/', 1)
                if len(parts) == 2:
                    owner, repo_name = parts[0], parts[1]
                    cloned = await SandboxArchiveService.clone_repo(
                        httpx_client=httpx_client,
                        agent_server_url=agent_url,
                        session_api_key=sandbox.session_api_key,
                        repo_owner=owner,
                        repo_name=repo_name,
                        branch=branch,
                    )
                    logger.info(
                        '[Automation] Restore step 4.5/6: clone result=%s',
                        cloned,
                    )

            logger.info('[Automation] Restore step 4/6: calling restore_into_sandbox...')
            ok = await archive_svc.restore_into_sandbox(
                agent_server_url=agent_url,
                session_api_key=sandbox.session_api_key,
                s3_key=archived.archive_location,
                conversation_id=conversation_id,
            )
            logger.info(
                '[Automation] Restore step 4/6: restore_into_sandbox result=%s',
                ok,
            )
            if not ok:
                logger.error('[Automation] Restore step 4/6: FAILED, cleaning up sandbox')
                await sandbox_service.delete_sandbox(sandbox.id)
                return False

            # Start the conversation (resume path activates automatically)
            logger.info(
                '[Automation] Restore step 5/6: POST /api/conversations '
                '(conversation_id=%s, max_iterations=%d)',
                conversation_id, archived.max_iterations or 500,
            )

            # Fetch agent_settings from the sandbox so the request passes
            # Pydantic validation (agent or agent_settings is required).
            # On resume ConversationState.create() overwrites the agent
            # with the one from the persisted base_state.json, so the
            # sandbox's current settings are only used for validation.
            # PersistedSettings always has a default agent_settings, so
            # this will always return valid settings even without an
            # API key configured.
            settings_resp = await httpx_client.get(
                f'{agent_url}/api/settings',
                headers={'X-Session-API-Key': sandbox.session_api_key},
                timeout=15.0,
            )
            settings_resp.raise_for_status()
            agent_settings = settings_resp.json()['agent_settings']
            logger.info(
                '[Automation] Restore step 5/6: fetched agent_settings '
                'from sandbox'
            )

            start_req: dict[str, object] = {
                'conversation_id': conversation_id,
                'workspace': {'working_dir': '/workspace/project'},
                'max_iterations': archived.max_iterations or 500,
                'agent_settings': agent_settings,
            }

            resp = await httpx_client.post(
                f'{agent_url}/api/conversations',
                json=start_req,
                headers={'X-Session-API-Key': sandbox.session_api_key},
                timeout=120.0,
            )
            logger.info(
                '[Automation] Restore step 5/6: POST /api/conversations → %s',
                resp.status_code,
            )
            resp.raise_for_status()

            # Forward the new comment message
            logger.info('[Automation] Restore step 6/6: posting comment event...')
            comment = payload.get('comment', {})
            comment_body = (comment.get('body', '') or '').strip()
            user = comment.get('author', {}).get('displayName', 'User')

            # Sanitize
            is_dangerous, labels = has_dangerous_patterns(
                comment_body, field_name='jira_existing_comment'
            )
            if is_dangerous:
                logger.warning(
                    '[Security] Rejecting dangerous comment on %s: %s',
                    issue_key, labels,
                )
                return False

            message_text = render_prompt(
                'jira_existing_conversation.j2',
                user=user,
                comment=comment_body,
                issue_key=issue_key,
            )
            await httpx_client.post(
                f'{agent_url}/api/conversations/{conversation_id}/events',
                json={
                    'role': 'user',
                    'content': [{'type': 'text', 'text': message_text}],
                    'run': True,
                },
                headers={
                    'X-Session-API-Key': sandbox.session_api_key,
                },
                timeout=60.0,
            )

            # Link the new sandbox to the conversation
            if conv_info:
                conv_info = conv_info.model_copy(
                    update={'sandbox_id': sandbox.id}
                )
                await info_service.save_app_conversation_info(conv_info)

            logger.info(
                f'[Automation] Restored and resumed conversation '
                f'{conversation_id} from archive {archived.archive_location}'
            )
            return True

    except Exception:
        logger.error(
            f'[Automation] Archive restore failed for {issue_key}',
            exc_info=True,
        )
        return False
