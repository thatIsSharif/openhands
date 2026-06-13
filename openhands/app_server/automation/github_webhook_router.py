"""GitHub webhook router - handles incoming GitHub webhook events.

Endpoint: POST /api/v1/webhooks/github

Primary Event: pull_request_review_comment
Future Events: pull_request_review, pull_request, issue_comment

Flow:
1. Validate webhook signature
2. Parse event type
3. Process asynchronously via BackgroundTasks
4. Return 202 accepted immediately
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Request

from openhands.agent_server.models import OpenHandsModel
from openhands.app_server.automation.github_automation_service import (
    GitHubAutomationService,
    verify_github_signature,
)
from openhands.app_server.utils.logger import openhands_logger as logger

router = APIRouter(prefix='/webhooks/github', tags=['automation'])


class GitHubWebhookResponse(OpenHandsModel):
    """Response model for GitHub webhook endpoint."""

    status: str
    execution_id: str | None = None
    conversation_id: str | None = None
    pr_number: int | None = None
    repository: str | None = None
    reason: str | None = None
    error: str | None = None


@router.post('')
async def handle_github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> GitHubWebhookResponse:
    """Handle an incoming GitHub webhook event.

    Primary: pull_request_review_comment events.
    Validates the signature, extracts event data, and schedules
    background processing. Returns immediately.
    """
    event_type = request.headers.get('X-GitHub-Event', '')
    delivery_id = request.headers.get('X-GitHub-Delivery', '')
    body = await request.body()
    payload = await request.json()

    logger.info(
        f'[Automation] GitHub webhook received: {event_type} '
        f'(delivery: {delivery_id})',
    )

    # Only process review comment events
    if event_type != 'pull_request_review_comment':
        return GitHubWebhookResponse(
            status='ignored',
            reason=f'Unsupported event: {event_type}',
        )

    # Verify signature (optional - requires GITHUB_WEBHOOK_SECRET env var)
    import os

    github_secret = os.environ.get('GITHUB_WEBHOOK_SECRET', '')
    if github_secret:
        signature = request.headers.get('X-Hub-Signature-256')
        if not verify_github_signature(body, signature, github_secret):
            logger.warning('[Automation] Invalid GitHub webhook signature')
            return GitHubWebhookResponse(
                status='rejected', reason='Invalid signature'
            )

    # Schedule background processing
    background_tasks.add_task(
        _process_github_review_comment, payload, request
    )

    return GitHubWebhookResponse(status='accepted')


async def _process_github_review_comment(
    payload: dict,
    request: Request,
) -> None:
    """Process a GitHub review comment event in the background.

    Creates a new execution and OpenHands conversation
    outside of the webhook request-response cycle.
    """
    from openhands.app_server.automation.execution_service import (
        ExecutionService,
    )
    from openhands.app_server.automation.execution_store import ExecutionStore
    from openhands.app_server.automation.github_automation_service import (
        GitHubAutomationService,
    )
    from openhands.app_server.automation.openhands_client import (
        OpenHandsClient,
    )

    try:
        # Build services using OSS DI
        store = ExecutionStore()
        execution_service = ExecutionService(store=store)
        openhands_client = OpenHandsClient()
        github_service = GitHubAutomationService(
            execution_service=execution_service,
            openhands_client=openhands_client,
        )

        result = await github_service.process_review_comment(
            payload=payload,
            state=request.state,
            request=request,
        )

        logger.info(
            f'[Automation] GitHub event processed: {result.get("status")} '
            f'(execution: {result.get("execution_id", "N/A")})',
        )
    except Exception as e:
        logger.error(
            f'[Automation] GitHub background processing failed: {e}'
        )
