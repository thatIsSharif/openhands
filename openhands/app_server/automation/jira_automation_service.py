"""Jira automation service - processes jira:issue_created webhook events.

Handles:
- Webhook signature verification (HMAC-SHA256)
- Event ID computation for idempotency
- Issue data extraction
- Branch name generation
- Repository resolution (Jira project → GitHub repo mapping)
- Execution and conversation creation
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import SourceType
from .execution_service import ExecutionService
from .execution_store import ExecutionStore
from .openhands_client import OpenHandsClient
from .repository_resolver import (
    JiraProjectRepositoryResolver,
    RepositoryNotResolvedError,
)

JIRA_WEBHOOK_EVENTS = frozenset({'jira:issue_created', 'jira:issue_updated'})

JIRA_TEMPLATE_PATH = (
    'openhands/app_server/integrations/templates/resolver/automation/'
    'jira_new_conversation.j2'
)


def verify_jira_signature(
    body: bytes, signature_header: str | None, secret: str
) -> bool:
    """Verify Jira webhook HMAC-SHA256 signature.

    Jira sends signatures in the format: sha256=<hex_digest>
    """
    if not signature_header:
        return False

    parts = signature_header.split('=', 1)
    if len(parts) != 2 or parts[0] != 'sha256':
        return False

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(parts[1], expected)


def compute_jira_event_id(payload: dict) -> str:
    """Compute a deterministic event ID for idempotency.

    Combines the webhook event type, issue ID, and timestamp.
    """
    webhook_event = payload.get('webhookEvent', '')
    issue_id = payload.get('issue', {}).get('id', '')
    timestamp = payload.get('timestamp', 0)
    raw = f'{webhook_event}:{issue_id}:{timestamp}'
    return hashlib.sha256(raw.encode()).hexdigest()


def extract_jira_issue_data(
    payload: dict,
) -> dict | None:
    """Extract issue metadata from a Jira webhook payload.

    Returns dict with keys: issue_key, summary, description, issue_type,
    priority, reporter, labels, project_key.
    Returns None if issue_key is missing.
    """
    issue = payload.get('issue', {})
    issue_key = issue.get('key')
    if not issue_key:
        return None

    fields = issue.get('fields', {})
    project = fields.get('project', {}) or {}

    return {
        'issue_key': issue_key,
        'summary': fields.get('summary', ''),
        'description': fields.get('description') or '',
        'issue_type': (fields.get('issuetype', {}) or {}).get('name', ''),
        'priority': (fields.get('priority', {}) or {}).get('name', ''),
        'reporter': (
            (fields.get('reporter', {}) or {}).get('displayName', '')
        ),
        'labels': fields.get('labels') or [],
        'project_key': project.get('key', ''),
    }


def extract_jira_project_key(payload: dict) -> str | None:
    """Extract the Jira project key from a webhook payload.

    The project key is nested in issue.fields.project.key.
    """
    return (
        payload.get('issue', {})
        .get('fields', {})
        .get('project', {})
        .get('key')
    )


def generate_jira_branch_name(
    issue_key: str,
    issue_type: str | None,
    summary: str,
) -> str:
    """Generate a deterministic branch name from Jira issue data.

    Format: {type}/{ISSUE-KEY}-{summary-slug}

    Type mapping:
    - Bug → bugfix
    - Story → feature
    - Task → feature
    - Improvement → feature
    - default → feature
    """
    type_lower = (issue_type or '').lower()

    if 'bug' in type_lower:
        prefix = 'bugfix'
    else:
        prefix = 'feature'

    slug = re.sub(r'[^a-zA-Z0-9\s-]', '', summary)
    slug = re.sub(r'[-\s]+', '-', slug).strip('-').lower()
    if len(slug) > 50:
        slug = slug[:50].rstrip('-')

    return f'{prefix}/{issue_key}-{slug}'


@dataclass
class JiraAutomationService:
    """Processes Jira issue webhook events.

    Flow:
    1. Verify webhook signature
    2. Compute event ID for idempotency
    3. Extract issue data
    4. Resolve target repository (custom field → project mapping → fail)
    5. Create execution record
    6. Generate branch name
    7. Create OpenHands conversation
    """

    execution_service: ExecutionService
    openhands_client: OpenHandsClient
    repo_resolver: JiraProjectRepositoryResolver | None = None

    async def process_issue_created(
        self,
        payload: dict,
        state,
        request=None,
    ) -> dict:
        """Process a jira:issue_created webhook event.

        Returns a dict with execution_id and status for the webhook response.
        """
        # Extract issue data
        issue_data = extract_jira_issue_data(payload)
        if not issue_data:
            logger.warning('[Automation] Jira webhook: missing issue key')
            return {
                'status': 'skipped',
                'reason': 'Missing issue key in payload',
            }

        issue_key = issue_data['issue_key']
        summary = issue_data['summary']
        project_key = issue_data['project_key']

        # Resolve target repository
        repository_str: str | None = None
        owner: str | None = None
        default_branch: str | None = None

        if project_key:
            resolver = self.repo_resolver or JiraProjectRepositoryResolver(
                store=self.execution_service.store
            )
            try:
                resolved = await resolver.resolve(
                    jira_project_key=project_key,
                    issue_payload=payload,
                )
                repository_str = resolved.repository
                owner = resolved.owner
                default_branch = resolved.default_branch
                logger.info(
                    f'[Automation] Resolved repository for {project_key}: '
                    f'{repository_str} (via {resolved.resolved_by})',
                    extra=build_log_context(
                        execution_id='',
                        jira_issue_key=issue_key,
                        repository=repository_str,
                    ),
                )
            except RepositoryNotResolvedError as e:
                logger.error(
                    f'[Automation] Repository resolution failed: {e}',
                    extra=build_log_context(
                        execution_id='',
                        jira_issue_key=issue_key,
                    ),
                )
                return {
                    'status': 'failed',
                    'issue_key': issue_key,
                    'error': str(e),
                }
        else:
            logger.error(
                '[Automation] Jira webhook: no project key in payload',
                extra=build_log_context(
                    execution_id='',
                    jira_issue_key=issue_key,
                ),
            )
            return {
                'status': 'failed',
                'issue_key': issue_key,
                'error': 'No Jira project key in webhook payload',
            }

        # Idempotency: compute event ID
        event_id = compute_jira_event_id(payload)

        # Generate branch name
        branch = generate_jira_branch_name(
            issue_key, issue_data['issue_type'], summary
        )

        # Create execution record with repository info
        execution_record, is_new = await self.execution_service.create_execution(
            source_type=SourceType.JIRA,
            source_event_id=event_id,
            jira_issue_key=issue_key,
            branch=branch,
            repository=repository_str,
        )

        # Skip if duplicate
        if not is_new:
            return {
                'status': 'duplicate',
                'execution_id': execution_record.execution_id,
                'issue_key': issue_key,
            }

        execution_id = execution_record.execution_id

        # Enqueue as RECEIVED → QUEUED
        await self.execution_service.transition_state(
            execution_id, 'QUEUED'  # type: ignore[arg-type]
        )

        # Build prompt from template with full context
        prompt = (
            f'You are working on Jira issue {issue_key}. '
            f'Title: {summary}\n\n'
            f'Description:\n{issue_data["description"]}\n\n'
            f'Issue Type: {issue_data["issue_type"]}\n'
            f'Priority: {issue_data["priority"]}\n'
            f'Reporter: {issue_data["reporter"]}\n\n'
            f'Target repository: {repository_str}\n'
            f'Default branch: {default_branch or "main"}\n\n'
            f'Please create a branch named "{branch}" from '
            f'{default_branch or "main"} and implement '
            f'the required changes. Create a pull request against '
            f'{default_branch or "main"} when done.'
        )

        # Create OpenHands conversation
        conversation_id = await self.openhands_client.create_conversation(
            state=state,
            request=request,
            prompt=prompt,
            title=f'[Automation] Jira {issue_key}',
            execution_id=execution_id,
            jira_issue_key=issue_key,
            repository=repository_str,
        )

        if conversation_id:
            # Transition to RUNNING
            await self.execution_service.transition_state(
                execution_id,
                'RUNNING',  # type: ignore[arg-type]
                conversation_id=conversation_id,
            )
            return {
                'status': 'running',
                'execution_id': execution_id,
                'conversation_id': conversation_id,
                'issue_key': issue_key,
                'repository': repository_str,
            }
        else:
            await self.execution_service.transition_state(
                execution_id,
                'FAILED',  # type: ignore[arg-type]
                error_message='Failed to create OpenHands conversation',
            )
            return {
                'status': 'failed',
                'execution_id': execution_id,
                'issue_key': issue_key,
                'error': 'Failed to create conversation',
            }
