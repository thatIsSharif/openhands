"""Jira webhook router - handles incoming Jira webhook events.

Endpoint: POST /api/v1/webhooks/jira

Flow:
1. Validate webhook signature
2. Parse event type
3. Process asynchronously via BackgroundTasks
4. Return 202 accepted immediately
"""

from __future__ import annotations

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

    # Only process issue creation events
    if webhook_event not in (
        'jira:issue_created',
        'jira:issue_updated',
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


async def _process_jira_event(
    payload: dict,
    request: Request,
) -> None:
    """Process a Jira webhook event in the background."""

    try:
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
            request=request,
        )

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
