"""
GitHub webhook router - handles incoming GitHub webhook events.

Endpoint: POST /api/v1/webhooks/github

Accepted Events:
- pull_request_review (with action=submitted) — preferred, fires once per review
- pull_request_review_comment — legacy, fires per inline comment

Flow:
1. Validate webhook signature
2. Parse event type
3. Route to appropriate handler
4. Process asynchronously via BackgroundTasks
5. Return 202 accepted immediately
"""

from __future__ import annotations

import json
import traceback

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from openhands.agent_server.models import OpenHandsModel
from openhands.app_server.automation.execution_service import (
    ExecutionService,
)
from openhands.app_server.automation.execution_store import ExecutionStore
from openhands.app_server.automation.github_automation_service import (
    GitHubAutomationService,
    verify_github_signature,
)
from openhands.app_server.automation.input_sanitizer import (
    has_dangerous_patterns,
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
from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER, SandboxStatus
from openhands.app_server.services.injector import InjectorState
from openhands.app_server.user.specifiy_user_context import ADMIN, USER_CONTEXT_ATTR
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)
from openhands.app_server.utils.github import add_pr_comment
from openhands.app_server.utils.logger import openhands_logger as logger
from openhands.app_server.utils.sandbox_utils import pause_sandbox

from .input_sanitizer import build_rejection_message


def _get_agent_url_from_sandbox(sandbox) -> str | None:
    """Extract the agent server URL from a sandbox's exposed URLs."""
    for exposed_url in sandbox.exposed_urls or []:
        if exposed_url.name == AGENT_SERVER:
            return exposed_url.url
    return None


router = APIRouter(prefix='/git/github/webhook', tags=['automation'])


class GitHubWebhookResponse(OpenHandsModel):
    """Response model for GitHub webhook endpoint."""

    status: str
    execution_id: str | None = None
    conversation_id: str | None = None
    pr_number: int | None = None
    repository: str | None = None
    reason: str | None = None
    error: str | None = None


class GitHubCommentRequest(BaseModel):
    """Request model for posting a comment on a GitHub PR."""

    repository: str
    pr_number: int
    body: str


def _is_pull_request_review_submitted(payload: dict) -> bool:
    """Check if the payload is a pull_request_review event with action=submitted."""
    return payload.get('action') == 'submitted'


@router.post('')
async def handle_github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> GitHubWebhookResponse:
    """Handle GitHub review webhooks.

    Only processes pull_request_review events when the review
    has been submitted. Inline review comments are ignored.
    """
    event_type = request.headers.get('X-GitHub-Event', '')
    delivery_id = request.headers.get('X-GitHub-Delivery', '')
    body = await request.body()
    payload = json.loads(body) if body else {}

    logger.info(
        f'[Automation] GitHub webhook received: {event_type} (delivery: {delivery_id})',
    )

    # Only accept review submissions
    if event_type != 'pull_request_review':
        return GitHubWebhookResponse(
            status='ignored',
            reason=f'Unsupported event: {event_type}',
        )

    # Only accept submitted reviews
    if payload.get('action') != 'submitted':
        return GitHubWebhookResponse(
            status='ignored',
            reason=f'Unsupported action: {payload.get("action")}',
        )

    review_state = payload.get('review', {}).get('state', '').lower()
    logger.info(f'[Automation] Review submitted with state: {review_state}')

    if review_state not in (
        'approved',
        'changes_requested',
    ):
        return GitHubWebhookResponse(
            status='ignored',
            reason=f'Unsupported review state: {review_state}',
        )

    repo = payload.get('repository', {})
    owner = repo.get('owner', {}).get('login')
    repository = repo.get('name')

    store = ExecutionStore()

    mapping = await store.get_repository_mapping(
        owner=owner,
        repository=repository,
    )

    github_secret = mapping.github_webhook_secret if mapping else None
    if github_secret:
        signature = request.headers.get('X-Hub-Signature-256')

        if not verify_github_signature(
            body,
            signature,
            github_secret,
        ):
            logger.warning('[Automation] Invalid GitHub webhook signature')

            return GitHubWebhookResponse(
                status='rejected',
                reason='Invalid signature',
            )

    background_tasks.add_task(
        _process_github_review_submitted,
        payload,
        delivery_id,
        request,
    )

    return GitHubWebhookResponse(
        status='accepted',
    )


async def _run_github_background(
    handler_name: str,
    handler_method: str,
    payload: dict,
    delivery_id: str,
    request: Request,
) -> None:
    """Run a GitHub automation handler in the background.

    Args:
        handler_name: Human-readable name for logging.
        handler_method: Attribute name of the method to call on
            ``GitHubAutomationService`` (e.g. ``"process_review_submitted"``).
        payload: The webhook payload.
        delivery_id: The ``X-GitHub-Delivery`` header value.
        request: The incoming FastAPI request.
    """
    try:
        store = ExecutionStore()
        execution_service = ExecutionService(store=store)
        openhands_client = OpenHandsClient()
        github_service = GitHubAutomationService(
            execution_service=execution_service,
            openhands_client=openhands_client,
        )

        method = getattr(github_service, handler_method)
        result = await method(
            payload=payload,
            state=request.state,
            request=request,
            delivery_id=delivery_id,
        )

        logger.info(
            f'[Automation] GitHub {handler_name} processed: '
            f'{result.get("status")} '
            f'(execution: {result.get("execution_id", "N/A")})',
        )
    except Exception as e:
        logger.error(f'[Automation] GitHub {handler_name} processing failed: {e}')


async def _process_github_review_comment(
    payload: dict,
    delivery_id: str,
    request: Request,
) -> None:
    """Process a pull_request_review_comment event in the background."""
    await _run_github_background(
        'review_comment',
        'process_review_comment',
        payload,
        delivery_id,
        request,
    )


async def _process_github_review_submitted(
    payload: dict,
    delivery_id: str,
    request: Request,
) -> None:
    """Process a pull_request_review (submitted) event in the background.

    Checks if a conversation already exists for the PR number. If found,
    resumes the sandbox and forwards the review to the existing conversation.
    Otherwise, falls through to the default behavior (new conversation).
    """
    repo_data = payload.get('repository', {})
    pr_data = payload.get('pull_request', {})
    review_data = payload.get('review', {}) or {}
    sender_data = payload.get('sender', {})

    full_name = repo_data.get('full_name', '')
    pr_number = pr_data.get('number')
    review_comment = review_data.get('body', '') or ''
    reviewer = sender_data.get('login', '')
    review_state = review_data.get('state', '').lower()

    if not full_name or not pr_number:
        logger.info(
            '[Automation] GitHub review event missing repository or PR number, '
            'falling through to new conversation creation'
        )
        await _run_github_background(
            'review_submitted',
            'process_review_submitted',
            payload,
            delivery_id,
            request,
        )
        return

    # Extract the PR html_url for conversation lookup
    pr_url = pr_data.get('html_url', '')
    if not pr_url:
        logger.info(
            f'[Automation] PR html_url not found for PR #{pr_number}, '
            'falling through to new conversation creation'
        )
        await _run_github_background(
            'review_submitted',
            'process_review_submitted',
            payload,
            delivery_id,
            request,
        )
        return

    logger.info(
        f'[Automation] Checking for existing conversation for '
        f'{full_name} PR #{pr_number} ({pr_url})'
    )

    # Look up existing conversation by PR URL (stored in github_pr column)
    async with get_app_conversation_info_service(
        request.state, request
    ) as info_service:
        conversation = await info_service.get_conversation_by_pr_url(pr_url)

    if not conversation:
        logger.info(
            f'[Automation] No existing conversation found for PR #{pr_number}, '
            'creating a new one'
        )
        await _run_github_background(
            'review_submitted',
            'process_review_submitted',
            payload,
            delivery_id,
            request,
        )
        return

    conversation_id = conversation.id
    sandbox_id = conversation.sandbox_id
    logger.info(
        f'[Automation] Found existing conversation {conversation_id} for '
        f'PR #{pr_number} (sandbox: {sandbox_id}), reusing it'
    )

    # Resume the sandbox if needed
    async with get_sandbox_service(request.state, request) as sandbox_service:
        sandbox = await sandbox_service.get_sandbox(sandbox_id)
        if sandbox is None:
            logger.warning(
                f'[Automation] Sandbox {sandbox_id} for conversation '
                f'{conversation_id} not found, creating new conversation'
            )
            await _run_github_background(
                'review_submitted',
                'process_review_submitted',
                payload,
                delivery_id,
                request,
            )
            return

        if sandbox.status == SandboxStatus.PAUSED:
            logger.info(
                f'[Automation] Resuming sandbox {sandbox_id} for PR #{pr_number}'
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
                    f'after 60 seconds for PR #{pr_number}'
                )
                return

            # Refresh sandbox info after resume
            sandbox = await sandbox_service.get_sandbox(sandbox_id)

        elif sandbox.status == SandboxStatus.MISSING:
            logger.warning(
                f'[Automation] Sandbox {sandbox_id} for PR #{pr_number} '
                'is missing, cannot resume'
            )
            return

    # Send the review as a message to the existing conversation
    async with get_httpx_client(request.state, request) as httpx_client:
        try:
            # Get fresh sandbox info for the agent server URL and session key
            async with get_sandbox_service(request.state, request) as sandbox_service:
                sandbox = await sandbox_service.get_sandbox(sandbox_id)

            if not sandbox or not sandbox.exposed_urls:
                logger.warning(
                    f'[Automation] Cannot send message for PR #{pr_number}: '
                    'sandbox has no exposed URLs'
                )
                return

            agent_server_url = _get_agent_url_from_sandbox(sandbox)
            if not agent_server_url:
                logger.warning(
                    f'[Automation] Cannot send message for PR #{pr_number}: '
                    'no agent server URL found'
                )
                return

            agent_server_url = replace_localhost_hostname_for_docker(agent_server_url)

            # Build the review message from the existing-conversation template
            state_label = {
                'approved': 'Approved',
                'changes_requested': 'Changes Requested',
                'comment': 'Comment',
            }.get(review_state, f'Review ({review_state})')

            # ── Input sanitization (Layer 1) ────────────────────────
            is_dangerous, labels = has_dangerous_patterns(
                review_comment, field_name='github_existing_review_comment'
            )
            if is_dangerous:
                logger.warning(
                    '[Security] Rejecting review on %s PR #%d '
                    'by %s (existing conversation): dangerous patterns=%s',
                    full_name, pr_number, reviewer, labels,
                )
                add_pr_comment(full_name, pr_number, build_rejection_message(review_comment))
                return

            message_text = render_prompt(
                'github_review_submitted_existing_conversation.j2',
                state_label=state_label,
                full_name=full_name,
                pr_url=pr_url,
                reviewer=reviewer,
                review_comment=review_comment,
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
                f'[Automation] Review for PR #{pr_number} forwarded to '
                f'conversation {conversation_id}'
            )

        except Exception:
            logger.error(
                f'[Automation] Failed to send message for PR #{pr_number} '
                f'to conversation {conversation_id}: '
                f'{traceback.format_exc()}'
            )


@router.post('/comment')
async def post_github_pr_comment(
    req: GitHubCommentRequest,
    request: Request,
) -> dict:
    """Post a comment on a GitHub PR and pause the sandbox.

    LLM calls this to post review follow-up comments to the PR.
    The function handles the GitHub API call and pauses the
    sandbox after task completion.
    """
    result = add_pr_comment(req.repository, req.pr_number, req.body)
    comment_id = result.get('id', '')

    # Pause sandbox after task completion
    try:
        pr_url = f'https://github.com/{req.repository}/pull/{req.pr_number}'
        state = InjectorState()
        setattr(state, USER_CONTEXT_ATTR, ADMIN)
        async with get_app_conversation_info_service(state, request) as info_service:
            conversation = await info_service.get_conversation_by_pr_url(pr_url)

            if conversation:
                # Ensure github_pr is populated for backward compatibility
                if (
                    not conversation.github_pr
                    or pr_url not in conversation.github_pr
                ):
                    from openhands.app_server.app_conversation.app_conversation_models import (
                        AppConversationInfo,
                    )

                    github_pr = (
                        list(conversation.github_pr)
                        if conversation.github_pr
                        else []
                    )
                    if pr_url not in github_pr:
                        github_pr.append(pr_url)

                    updated_info = AppConversationInfo(
                        id=conversation.id,
                        created_by_user_id=conversation.created_by_user_id,
                        sandbox_id=conversation.sandbox_id,
                        selected_repository=conversation.selected_repository,
                        selected_branch=conversation.selected_branch,
                        git_provider=conversation.git_provider,
                        title=conversation.title,
                        trigger=conversation.trigger,
                        pr_number=conversation.pr_number,
                        llm_model=conversation.llm_model,
                        agent_kind=conversation.agent_kind,
                        metrics=conversation.metrics,
                        parent_conversation_id=conversation.parent_conversation_id,
                        sub_conversation_ids=conversation.sub_conversation_ids,
                        public=conversation.public,
                        tags=conversation.tags,
                        jira_issue_key=conversation.jira_issue_key,
                        github_pr=github_pr,
                        created_at=conversation.created_at,
                        updated_at=conversation.updated_at,
                    )
                    await info_service.save_app_conversation_info(
                        updated_info
                    )
                    logger.info(
                        '[Automation] Updated github_pr for '
                        'conversation %s with %s',
                        conversation.id,
                        pr_url,
                    )

                if conversation.sandbox_id:
                    await pause_sandbox(
                        conversation.sandbox_id, state, request
                    )
    except Exception:
        logger.error(
            '[Automation] Failed to pause sandbox for PR %s #%d: %s',
            req.repository,
            req.pr_number,
            traceback.format_exc(),
        )

    return {'status': 'ok', 'comment_id': comment_id}
