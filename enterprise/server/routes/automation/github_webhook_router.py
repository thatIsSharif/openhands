"""GitHub webhook router for the automation platform.

POST /api/v1/github/webhook - Receive GitHub pull_request_review_comment webhooks

Pattern: Validate → Check idempotency → Create execution → Return 202
→ Background task processes execution.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse

from openhands.app_server.utils.logger import openhands_logger as logger

from integrations.automation.github_automation_service import (
    GitHubAutomationService,
    extract_github_review_data,
)

automation_github_router = APIRouter(prefix='/api/v1/github')

# Module-level singleton (same pattern as existing integration routers)
_github_automation_service: GitHubAutomationService | None = None


def _get_github_automation_service() -> GitHubAutomationService:
    global _github_automation_service
    if _github_automation_service is None:
        _github_automation_service = GitHubAutomationService()
    return _github_automation_service


def _get_webhook_secret() -> str | None:
    return os.getenv('GITHUB_WEBHOOK_SECRET') or None


@automation_github_router.post('/webhook')
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
):
    """Receive GitHub webhook events for PR review comments.

    Validates the webhook, creates an execution record, and queues
    background processing. Returns 202 Accepted immediately.
    """
    # Only process pull_request_review_comment events
    if x_github_event != 'pull_request_review_comment':
        return JSONResponse(
            status_code=200,
            content={
                'status': 'SKIPPED',
                'message': f'Unhandled event type: {x_github_event}',
            },
        )

    delivery_id = x_github_delivery or 'unknown'
    service = _get_github_automation_service()
    webhook_secret = _get_webhook_secret()

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={'error': 'Invalid JSON payload'},
        )

    result = await service.handle_review_comment(
        payload=payload,
        delivery_id=delivery_id,
        webhook_secret=webhook_secret,
        signature=x_hub_signature_256,
    )

    if result.get('status') == 'REJECTED':
        return JSONResponse(
            status_code=400,
            content=result,
        )

    if result.get('status') == 'DUPLICATE':
        return JSONResponse(
            status_code=409,
            content=result,
        )

    # Extract review data for background execution
    review_data = extract_github_review_data(payload)
    if review_data:
        execution_id = result['execution_id']

        background_tasks.add_task(
            service.execute_review_comment,
            execution_id=execution_id,
            repository=review_data['repository'],
            pr_number=review_data['pr_number'],
            branch=review_data['branch'],
            pr_title=review_data['pr_title'],
            pr_body=review_data['pr_body'],
            review_comment=review_data['review_comment'],
            reviewer=review_data['reviewer'],
        )
        logger.info(
            f'[GitHubWebhook] Queued background execution {execution_id} '
            f'for PR #{review_data["pr_number"]}'
        )

    return JSONResponse(
        status_code=202,
        content=result,
    )
