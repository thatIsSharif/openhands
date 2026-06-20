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

from fastapi import APIRouter, BackgroundTasks, Request

from openhands.agent_server.models import OpenHandsModel
from openhands.app_server.automation.execution_service import (
    ExecutionService,
)
from openhands.app_server.automation.execution_store import ExecutionStore
from openhands.app_server.automation.github_automation_service import (
    GitHubAutomationService,
    verify_github_signature,
)
from openhands.app_server.automation.openhands_client import (
    OpenHandsClient,
)
from openhands.app_server.utils.logger import openhands_logger as logger

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
    payload = await request.json()

    logger.info(
        f'[Automation] GitHub webhook received: {event_type} '
        f'(delivery: {delivery_id})',
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

    review_state = (
    payload.get('review', {})
    .get('state', '')
    .lower()
    )
    logger.info(
    f'[Automation] Review submitted with state: {review_state}')

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

    github_secret = (
        mapping.github_webhook_secret
        if mapping
        else None
    )
    if github_secret:
        signature = request.headers.get('X-Hub-Signature-256')

        if not verify_github_signature(
            body,
            signature,
            github_secret,
        ):
            logger.warning(
                '[Automation] Invalid GitHub webhook signature'
            )

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
        logger.error(
            f'[Automation] GitHub {handler_name} processing failed: {e}'
        )


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
    """Process a pull_request_review (submitted) event in the background."""
    await _run_github_background(
        'review_submitted',
        'process_review_submitted',
        payload,
        delivery_id,
        request,
    )
