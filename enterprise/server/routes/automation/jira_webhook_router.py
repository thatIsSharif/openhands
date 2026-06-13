"""Jira webhook router for the automation platform.

POST /api/v1/jira/webhook - Receive Jira issue webhooks

Pattern: Validate → Check idempotency → Create execution → Return 202
→ Background task processes execution.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse

from openhands.app_server.utils.logger import openhands_logger as logger

from integrations.automation.jira_automation_service import (
    JiraAutomationService,
    extract_jira_issue_data,
)

automation_jira_router = APIRouter(prefix='/api/v1/jira')

# Module-level singleton (same pattern as existing integration routers)
_jira_automation_service: JiraAutomationService | None = None


def _get_jira_automation_service() -> JiraAutomationService:
    global _jira_automation_service
    if _jira_automation_service is None:
        _jira_automation_service = JiraAutomationService()
    return _jira_automation_service


def _get_webhook_secret() -> str | None:
    return os.getenv('JIRA_WEBHOOK_SECRET') or None


@automation_jira_router.post('/webhook')
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(None),
):
    """Receive Jira issue webhook events.

    Validates the webhook, creates an execution record, and queues
    background processing. Returns 202 Accepted immediately.
    """
    service = _get_jira_automation_service()
    webhook_secret = _get_webhook_secret()

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={'error': 'Invalid JSON payload'},
        )

    webhook_event = payload.get('webhookEvent', '')
    if webhook_event != 'jira:issue_created':
        return JSONResponse(
            status_code=200,
            content={
                'status': 'SKIPPED',
                'message': f'Unhandled event type: {webhook_event}',
            },
        )

    result = await service.handle_issue_created(
        payload=payload,
        webhook_secret=webhook_secret,
        signature=x_hub_signature,
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

    # Extract issue data for background execution
    issue_data = extract_jira_issue_data(payload)
    if issue_data:
        execution_id = result['execution_id']

        background_tasks.add_task(
            service.execute_jira_issue,
            execution_id=execution_id,
            issue_key=issue_data['issue_key'],
            summary=issue_data['summary'],
            description=issue_data.get('description'),
            issue_type=issue_data.get('issue_type'),
        )
        logger.info(
            f'[JiraWebhook] Queued background execution {execution_id} '
            f'for issue {issue_data["issue_key"]}'
        )

    return JSONResponse(
        status_code=202,
        content=result,
    )
