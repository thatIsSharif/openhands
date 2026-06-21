"""Jira automation service - processes jira:issue_created webhook events.


Handles:
- Webhook signature verification (HMAC-SHA256)
- Event ID computation for idempotency
- Issue data extraction
- Repository extraction from Jira issue payload
- Branch name generation
- Execution and conversation creation
"""


from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import ExecutionState, SourceType
from .execution_service import ExecutionService
from .openhands_client import OpenHandsClient
from .prompt_renderer import render_prompt

JIRA_WEBHOOK_EVENTS = frozenset({'jira:issue_created', 'jira:issue_updated'})




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




_JIRA_REPOSITORY_FIELDS = [
    'customfield_10171',
    'repository',
]




def extract_jira_repository(payload: dict) -> str | None:
    """Extract the target repository from a Jira issue payload.


    Repository selection comes exclusively from the Jira issue itself.
    The repository field should contain an ``owner/repository`` string.


    Returns the repository string (e.g. ``thatIsSharif/workflow-engine``)
    or ``None`` if no repository field is found.
    """
    fields = payload.get('issue', {}).get('fields', {}) or {}


    for field_id in _JIRA_REPOSITORY_FIELDS:
        value = fields.get(field_id)
        if value is None:
            continue


        # Support both plain strings and objects with "value" key
        if isinstance(value, dict):
            value = value.get('value') or value.get('name')
        elif not isinstance(value, str):
            continue


        if value and isinstance(value, str):
            return value.strip()


    return None




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




def _validate_repository_format(repository: str) -> bool:
    """Validate that a repository string is in ``owner/repository`` format."""
    parts = repository.strip().split('/', 1)
    return len(parts) == 2 and bool(parts[0]) and bool(parts[1])




@dataclass
class JiraAutomationService:
    """Processes Jira issue webhook events.


    Flow:
    1. Verify webhook signature
    2. Compute event ID for idempotency
    3. Extract issue data and repository from issue payload
    4. Create execution record
    5. Generate branch name
    6. Create OpenHands conversation
    """


    execution_service: ExecutionService
    openhands_client: OpenHandsClient


    async def process_issue_created(
        self,
        payload: dict,
        state,
        request=None,
    ) -> dict:
        """Process a jira:issue_created webhook event.


        Repository selection comes exclusively from the Jira issue payload.
        The repository field must contain an ``owner/repository`` string.


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


        # Extract repository from the Jira issue payload
        repository_str = extract_jira_repository(payload)
        if not repository_str:
            logger.error(
                '[Automation] Jira webhook: repository field missing in '
                f'issue {issue_key}. Expected a repository field (e.g. '
                'customfield_10010) with value in "owner/repository" format.',
                extra=build_log_context(
                    execution_id='',
                    jira_issue_key=issue_key,
                ),
            )
            return {
                'status': 'failed',
                'issue_key': issue_key,
                'error': (
                    'Repository field missing in Jira issue. '
                    'Ensure the issue has a repository field '
                    '(e.g. customfield_10010) set.'
                ),
            }


        # Validate format: owner/repository
        if not _validate_repository_format(repository_str):
            logger.error(
                '[Automation] Jira webhook: invalid repository format in '
                f'issue {issue_key}: "{repository_str}". '
                'Expected "owner/repository" format.',
                extra=build_log_context(
                    execution_id='',
                    jira_issue_key=issue_key,
                    repository=repository_str,
                ),
            )
            return {
                'status': 'failed',
                'issue_key': issue_key,
                'error': (
                    f'Invalid repository format: "{repository_str}". '
                    'Expected format: "owner/repository".'
                ),
            }


        logger.info(
            f'[Automation] Resolved repository for {issue_key}: '
            f'{repository_str} (from issue payload)',
            extra=build_log_context(
                execution_id='',
                jira_issue_key=issue_key,
                repository=repository_str,
            ),
        )


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
            execution_id, ExecutionState.QUEUED
        )


        default_branch = 'main'


        # Build the full comment endpoint URL from the incoming request
        base_url = str(request.base_url).rstrip('/')
        comment_endpoint = f'{base_url}/api/v1/jira/start/comment'
        teams_notify_endpoint = f'{base_url}/api/v1/teams/notify'

        # Build prompt from template with full context
        prompt = render_prompt(
            'jira_new_conversation.j2',
            issue_key=issue_key,
            title=summary,
            issue_type=issue_data['issue_type'],
            priority=issue_data['priority'],
            reporter=issue_data['reporter'],
            description=issue_data['description'],
            repository=repository_str,
            default_branch=default_branch,
            branch=branch,
            comment_endpoint=comment_endpoint,
            teams_notify_endpoint=teams_notify_endpoint,
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
            branch=default_branch,
        )


        if conversation_id:
            # Transition to RUNNING
            await self.execution_service.transition_state(
                execution_id,
                ExecutionState.RUNNING,
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
                ExecutionState.FAILED,
                error_message='Failed to create OpenHands conversation',
            )
            return {
                'status': 'failed',
                'execution_id': execution_id,
                'issue_key': issue_key,
                'error': 'Failed to create conversation',
            }
