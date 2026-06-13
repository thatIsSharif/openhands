"""Jira automation service - processes Jira webhook events and creates executions.

This is a standalone service that does not extend the existing JiraManager,
keeping the automation platform concerns separate from the OAuth-based integration.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import ExecutionState, SourceType
from .execution_service import ExecutionService
from .execution_store import ExecutionStore
from .openhands_client import OpenHandsClient


def compute_jira_event_id(payload: dict) -> str:
    """Compute a unique event ID for Jira webhook idempotency.

    Uses webhookEvent + issue.id + webhook timestamp to create
    a deterministic event identifier.
    """
    webhook_event = payload.get('webhookEvent', '')
    issue = payload.get('issue', {})
    issue_id = issue.get('id', '')
    timestamp = payload.get('timestamp', int(datetime.now(timezone.utc).timestamp()))
    raw = f'jira:{webhook_event}:{issue_id}:{timestamp}'
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_jira_signature(
    payload_body: bytes,
    signature_header: str | None,
    webhook_secret: str,
) -> bool:
    """Verify HMAC-SHA256 signature for Jira webhook requests.

    Jira uses the x-hub-signature header with HMAC-SHA256.
    """
    if not signature_header:
        return False
    try:
        # Expected format: sha256=<hex-digest> or sha1=<hex-digest>
        if '=' in signature_header:
            _, expected_sig = signature_header.split('=', 1)
        else:
            expected_sig = signature_header

        computed = hmac.new(
            webhook_secret.encode(),
            payload_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, expected_sig)
    except Exception:
        return False


def extract_jira_issue_data(
    payload: dict,
) -> dict | None:
    """Extract normalized issue data from a Jira webhook payload.

    Returns a dict with issue_key, summary, description, issue_type,
    priority, reporter, and labels, or None if required fields are missing.
    """
    issue = payload.get('issue', {})
    issue_key = issue.get('key', '')

    if not issue_key:
        logger.warning('[JiraAutomation] Missing issue key in payload')
        return None

    fields = issue.get('fields', {})
    reporter_data = fields.get('reporter', {}) or {}

    return {
        'issue_key': issue_key,
        'summary': fields.get('summary', ''),
        'description': fields.get('description', ''),
        'issue_type': (
            fields.get('issuetype', {}).get('name', '')
            if fields.get('issuetype')
            else ''
        ),
        'priority': (
            fields.get('priority', {}).get('name', '')
            if fields.get('priority')
            else ''
        ),
        'reporter': reporter_data.get('displayName', ''),
        'labels': fields.get('labels', []),
    }


def generate_jira_branch_name(
    issue_key: str,
    issue_type: str | None,
    summary: str,
) -> str:
    """Generate a deterministic branch name for a Jira issue.

    Pattern: {issue_type}/{ISSUE-KEY}-{summary-slug}
    """
    import re

    # Determine prefix
    prefix = 'feature'
    if issue_type:
        type_lower = issue_type.lower()
        if 'bug' in type_lower:
            prefix = 'bugfix'
        elif 'task' in type_lower or 'story' in type_lower:
            prefix = 'feature'
        elif 'epic' in type_lower:
            prefix = 'epic'

    # Create slug from summary
    slug = re.sub(r'[^a-zA-Z0-9\s-]', '', summary.lower())
    slug = re.sub(r'[\s-]+', '-', slug).strip('-')
    slug = slug[:40].rstrip('-')

    return f'{prefix}/{issue_key}-{slug}'


class JiraAutomationService:
    """Processes Jira issue webhooks and manages the automation workflow."""

    def __init__(
        self,
        execution_service: ExecutionService | None = None,
        execution_store: ExecutionStore | None = None,
        openhands_client: OpenHandsClient | None = None,
    ) -> None:
        self._execution_service = execution_service or ExecutionService(
            store=execution_store or ExecutionStore()
        )
        self._execution_store = execution_store or ExecutionStore()
        self._openhands_client = openhands_client or OpenHandsClient()

    async def handle_issue_created(
        self,
        payload: dict,
        webhook_secret: str | None = None,
        signature: str | None = None,
    ) -> dict:
        """Handle a jira:issue_created webhook event.

        Steps:
        1. Verify signature (if secret configured)
        2. Compute event ID for idempotency
        3. Extract issue data
        4. Create execution record
        5. Queue execution as background task

        Returns:
            Response dict with execution_id and status.
        """
        # Signature verification
        if webhook_secret and signature:
            raw_body = json.dumps(payload).encode()
            if not verify_jira_signature(raw_body, signature, webhook_secret):
                logger.warning('[JiraAutomation] Invalid webhook signature')
                return {
                    'error': 'Invalid webhook signature',
                    'status': 'REJECTED',
                }

        # Compute idempotency key
        event_id = compute_jira_event_id(payload)

        # Extract issue data
        issue_data = extract_jira_issue_data(payload)
        if not issue_data:
            return {
                'error': 'Failed to extract issue data from payload',
                'status': 'REJECTED',
            }

        log_ctx = build_log_context(
            jira_issue_key=issue_data['issue_key'],
        )
        logger.info(
            f'[JiraAutomation] Processing issue {issue_data["issue_key"]}',
            extra=log_ctx,
        )

        # Create execution with idempotency check
        record, is_new = await self._execution_service.create_execution(
            source_type=SourceType.JIRA,
            source_event_id=event_id,
            jira_issue_key=issue_data['issue_key'],
        )

        if not is_new:
            return {
                'execution_id': record.execution_id,
                'status': 'DUPLICATE',
                'message': 'Webhook event already processed',
            }

        # Transition to QUEUED
        await self._execution_service.transition_state(
            record.execution_id, ExecutionState.QUEUED
        )

        # Persist Jira issue metadata
        await self._execution_store.upsert_jira_issue(
            issue_key=issue_data['issue_key'],
            summary=issue_data['summary'],
            description=issue_data.get('description'),
            issue_type=issue_data.get('issue_type'),
            priority=issue_data.get('priority'),
            reporter=issue_data.get('reporter'),
            labels=issue_data.get('labels'),
            webhook_event_id=event_id,
            execution_id=record.id,
        )

        return {
            'execution_id': record.execution_id,
            'status': 'RECEIVED',
            'message': 'Jira issue execution request received',
        }

    async def execute_jira_issue(
        self,
        execution_id: str,
        issue_key: str,
        summary: str,
        description: str | None = None,
        issue_type: str | None = None,
    ) -> None:
        """Execute a Jira issue automation in the background.

        This is called by the background task after the webhook returns.
        """
        log_ctx = build_log_context(
            execution_id=execution_id,
            jira_issue_key=issue_key,
        )

        try:
            # Transition to RUNNING
            await self._execution_service.transition_state(
                execution_id, ExecutionState.RUNNING
            )

            # Generate branch name
            branch_name = generate_jira_branch_name(issue_key, issue_type, summary)

            # Render template
            jinja_env = self._openhands_client.get_template_env()
            template = jinja_env.get_template('automation/jira_new_conversation.j2')
            prompt = template.render(
                issue_key=issue_key,
                issue_title=summary,
                issue_description=description or '',
                issue_type=issue_type or '',
                priority='',
                branch_name=branch_name,
            )

            # Create OpenHands conversation
            conversation_id = await self._openhands_client.create_conversation(
                execution_id=execution_id,
                prompt=prompt,
                jira_issue_key=issue_key,
            )

            if conversation_id:
                # Update execution with conversation ID
                await self._execution_service.transition_state(
                    execution_id,
                    ExecutionState.RUNNING,
                    conversation_id=conversation_id,
                )
                logger.info(
                    f'[JiraAutomation] Started execution {execution_id} '
                    f'with conversation {conversation_id}',
                    extra=log_ctx,
                )
            else:
                await self._execution_service.transition_state(
                    execution_id,
                    ExecutionState.FAILED,
                    error_message='Failed to create OpenHands conversation',
                )

        except Exception as e:
            logger.error(
                f'[JiraAutomation] Execution {execution_id} failed: {e}',
                extra=log_ctx,
            )
            await self._execution_service.transition_state(
                execution_id,
                ExecutionState.FAILED,
                error_message=str(e),
            )
