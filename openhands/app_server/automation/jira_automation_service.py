"""Jira automation service - processes jira:issue_created webhook events.

Handles:
- Webhook signature verification (HMAC-SHA256)
- Event ID computation for idempotency
- Issue data extraction
- Repository extraction from Jira issue payload
- Branch name generation
- Execution and conversation creation
- Multi-repository support (backend + frontend)
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from openhands.app_server.utils.jira import (
    add_comment,
    mark_issue_in_progress,
)
from openhands.app_server.utils.logger import openhands_logger as logger

from .complexity_analyzer import ComplexityAnalyzer
from .complexity_router import ComplexityRouter
from .correlation import build_log_context
from .execution_models import ExecutionState, SourceType
from .execution_service import ExecutionService
from .input_sanitizer import (
    build_rejection_message,
    has_dangerous_patterns,
    validate_jira_issue_key,
)
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
        'reporter': ((fields.get('reporter', {}) or {}).get('displayName', '')),
        'labels': fields.get('labels') or [],
        'project_key': project.get('key', ''),
    }


def extract_jira_project_key(payload: dict) -> str | None:
    """Extract the Jira project key from a webhook payload.

    The project key is nested in issue.fields.project.key.
    """
    return payload.get('issue', {}).get('fields', {}).get('project', {}).get('key')


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


async def _update_jira_issue_status(
    issue_key: str,
    execution_id: str,
) -> None:
    """Update Jira issue status when the agent starts working on it.

    Transitions the issue to 'In Progress' and adds a comment
    indicating OpenHands has started working.

    This is a best-effort operation: failures are logged but do
    not block the automation flow.
    """
    import traceback

    try:
        # Transition issue to In Progress
        result = mark_issue_in_progress(issue_key)
        if result:
            logger.info(
                '[Automation] Jira issue %s transitioned to In Progress '
                '(execution: %s)',
                issue_key, execution_id,
                extra=build_log_context(
                    execution_id=execution_id,
                    jira_issue_key=issue_key,
                ),
            )
        else:
            logger.warning(
                '[Automation] Could not transition Jira issue %s '
                'to In Progress - no matching transition found '
                '(execution: %s)',
                issue_key, execution_id,
                extra=build_log_context(
                    execution_id=execution_id,
                    jira_issue_key=issue_key,
                ),
            )

        # Add a comment that OpenHands has started working
        add_comment(
            issue_key,
            'OpenHands has started working on this issue. '
            f'(Execution ID: {execution_id})',
        )
        logger.info(
            '[Automation] Posted started-working comment on Jira issue %s '
            '(execution: %s)',
            issue_key, execution_id,
            extra=build_log_context(
                execution_id=execution_id,
                jira_issue_key=issue_key,
            ),
        )

    except Exception:
        logger.error(
            '[Automation] Failed to update Jira issue status for %s: %s',
            issue_key, traceback.format_exc(),
            extra=build_log_context(
                execution_id=execution_id,
                jira_issue_key=issue_key,
            ),
        )


@dataclass
class JiraAutomationService:
    """Processes Jira issue webhook events.

    Flow:
    1. Verify webhook signature
    2. Compute event ID for idempotency
    3. Extract issue data and repository from issue payload
    4. Create execution record
    5. Generate branch name
    6. Create OpenHands conversation with backend repo
    7. Secondary repos provided in prompt for manual cloning if needed
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

        Repository selection:
        - Backend repo is ALWAYS attached as primary
        - Frontend repo info is included in prompt for manual cloning if needed
        - Agent reads the ticket and decides what to do:
          * Backend only → make changes, raise PR
          * Frontend only → clone frontend manually, make changes, raise PR
          * Both → make changes in both, raise PRs for both

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

        project_key = extract_jira_project_key(payload)

        if not project_key:
            logger.error(
                f'[Automation] Jira webhook: missing project key in issue {issue_key}',
                extra=build_log_context(
                    execution_id='',
                    jira_issue_key=issue_key,
                ),
            )
            return {
                'status': 'failed',
                'issue_key': issue_key,
                'error': 'Missing project key in Jira payload',
            }

        # ── Resolve repositories from the DB table ────────────────
        repo_records = (
            await self.execution_service.store.get_jira_project_repos_by_project_key(
                project_key
            )
        )

        if not repo_records:
            logger.error(
                '[Automation] Jira webhook: no repositories configured for '
                f'project {project_key} (issue {issue_key}). '
                'Add entries via POST /api/v1/admin/jira-project-repos.',
                extra=build_log_context(
                    execution_id='',
                    jira_issue_key=issue_key,
                ),
            )
            return {
                'status': 'failed',
                'issue_key': issue_key,
                'error': (
                    f'No repositories configured for project '
                    f'"{project_key}". Please configure at least one '
                    'repository mapping via the admin API.'
                ),
            }

        logger.info(
            f'[Automation] Resolved {len(repo_records)} repo(s) for '
            f'{issue_key} (project={project_key}): '
            f'{", ".join(f"{r.owner}/{r.repository}" for r in repo_records)}',
            extra=build_log_context(
                execution_id='',
                jira_issue_key=issue_key,
            ),
        )

        # ── Determine primary repository ────────────────────────────
        # Backend is always the primary (attached)
        # All other repos are passed to the prompt for potential cloning
        backend_repo = None
        other_repos = []

        for repo in repo_records:
            repo_label = getattr(repo, 'label', None) or 'default'
            if repo_label.lower() == 'backend' or repo_label.lower() == 'default':
                if not backend_repo:
                    backend_repo = repo
                else:
                    other_repos.append(repo)
            else:
                other_repos.append(repo)

        # Fallback: if no backend label, use first as primary
        if not backend_repo and repo_records:
            backend_repo = repo_records[0]
            other_repos = repo_records[1:]
        elif backend_repo and backend_repo not in other_repos:
            # Ensure backend is not in other_repos
            other_repos = [r for r in other_repos if r != backend_repo]

        if not backend_repo:
            logger.error(
                f'[Automation] Jira webhook: no backend repo found for '
                f'project {project_key} (issue {issue_key})',
                extra=build_log_context(
                    execution_id='',
                    jira_issue_key=issue_key,
                ),
            )
            return {
                'status': 'failed',
                'issue_key': issue_key,
                'error': 'No backend repository found for project',
            }

        primary_repository = f'{backend_repo.owner}/{backend_repo.repository}'
        primary_label = getattr(backend_repo, 'label', None) or 'default'
        default_branch = getattr(backend_repo, 'default_branch', None) or 'main'

        # Build list of all other repos (for cloning if needed)
        other_repos_info = []
        for repo in other_repos:
            repo_info = {
                'owner': repo.owner,
                'repository': repo.repository,
                'branch': getattr(repo, 'default_branch', None) or 'main',
                'label': getattr(repo, 'label', None) or 'default',
            }
            other_repos_info.append(repo_info)
            logger.info(
                f'[Automation] Additional repo for {issue_key}: {repo.owner}/{repo.repository} ({repo_info["label"]})',
                extra=build_log_context(
                    execution_id='',
                    jira_issue_key=issue_key,
                ),
            )

        # Idempotency: compute event ID
        event_id = compute_jira_event_id(payload)

        # Generate branch name
        branch = generate_jira_branch_name(issue_key, issue_data['issue_type'], summary)

        # Create execution record with primary repository info
        execution_record, is_new = await self.execution_service.create_execution(
            source_type=SourceType.JIRA,
            source_event_id=event_id,
            jira_issue_key=issue_key,
            branch=branch,
            repository=primary_repository,
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

        # Build the full endpoint URLs from the incoming request
        base_url = str(request.base_url).rstrip('/')
        comment_endpoint = f'{base_url}/api/v1/jira/start/comment'
        token_usage_endpoint = f'{base_url}/api/v1/jira/start/token-usage'

        # ── Input sanitization (Layer 1) ────────────────────────────
        # Check all user-controlled text fields for dangerous patterns
        validated_issue_key = (
            issue_key if validate_jira_issue_key(issue_key) else 'INVALID-KEY'
        )
        for field_name, field_value in [
            # ('jira_summary', summary),
            # ('jira_description', issue_data['description']),
            # ('jira_issue_type', issue_data['issue_type']),
            # ('jira_priority', issue_data['priority']),
            ('jira_reporter', issue_data['reporter']),
        ]:
            is_dangerous, labels = has_dangerous_patterns(field_value, field_name)
            if is_dangerous:
                logger.warning(
                    '[Security] Rejecting Jira issue %s due to dangerous '
                    'patterns in %s: %s',
                    issue_key, field_name, labels,
                )
                add_comment(
                    issue_key,
                    build_rejection_message(field_value),
                )
                await self.execution_service.transition_state(
                    execution_id,
                    ExecutionState.FAILED,
                    error_message=(
                        f'Issue rejected: dangerous patterns in '
                        f'{field_name} ({", ".join(labels)})'
                    ),
                )
                return {
                    'status': 'rejected',
                    'execution_id': execution_id,
                    'issue_key': issue_key,
                    'repository': primary_repository,
                    'reason': f'Issue contains dangerous patterns in {field_name}',
                }

        # ── Complexity-based model routing ──────────────────────────
        llm_model: str | None = None

        router = ComplexityRouter.from_env()
        if router.is_enabled:
            analyzer = ComplexityAnalyzer.from_env()
            result = await analyzer.analyze(issue_data)
            if result:
                logger.info(
                    '[Automation] Jira %s complexity: %s (%s)',
                    issue_key,
                    result.complexity,
                    result.reasoning,
                )
                llm_model = router.resolve(result.complexity)
            else:
                logger.warning(
                    '[Automation] Complexity analysis failed for %s, '
                    'using default model',
                    issue_key,
                )

        # Build prompt from template with full context
        # Include all other repos for potential cloning
        prompt = render_prompt(
            'jira_new_conversation.j2',
            issue_key=validated_issue_key,
            title=summary,
            issue_type=issue_data['issue_type'],
            priority=issue_data['priority'],
            reporter=issue_data['reporter'],
            description=issue_data['description'],
            repository=primary_repository,
            repo_label=primary_label,
            default_branch=default_branch,
            branch=branch,
            comment_endpoint=comment_endpoint,
            token_usage_endpoint=token_usage_endpoint,
            other_repos=other_repos_info,
        )

        # Create OpenHands conversation with primary (backend) repository
        conversation_id = await self.openhands_client.create_conversation(
            state=state,
            request=request,
            prompt=prompt,
            title=f'[Automation] Jira {issue_key}',
            execution_id=execution_id,
            jira_issue_key=issue_key,
            repository=primary_repository,
            branch=default_branch,
            llm_model=llm_model,
        )

        if conversation_id:
            # Transition to RUNNING
            await self.execution_service.transition_state(
                execution_id,
                ExecutionState.RUNNING,
                conversation_id=conversation_id,
            )

            # ── Update Jira issue status ──────────────────────────────
            # Transition the issue to In Progress and add a comment
            # indicating OpenHands has started working on it.
            # These calls are best-effort: failures are logged but
            # do not block the overall automation flow.
            await _update_jira_issue_status(issue_key, execution_id)

            return {
                'status': 'running',
                'execution_id': execution_id,
                'conversation_id': conversation_id,
                'issue_key': issue_key,
                'repository': primary_repository,
                'other_repos': [
                    f'{r["owner"]}/{r["repository"]}' for r in other_repos_info
                ],
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
