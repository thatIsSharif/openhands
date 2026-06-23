"""Jira automation service - processes jira:issue_created webhook events.


Handles:
- Webhook signature verification (HMAC-SHA256)
- Event ID computation for idempotency
- Issue data extraction
- Repository resolution from jira_project_repositories table
- Branch name generation
- Execution and conversation creation

Design (multi-repo):
- Phase 1 (parallel via ``asyncio.gather``): create execution records
  (each uses its own DB session via ``ExecutionStore._get_session``)
- Phase 2 (sequential): create conversations one at a time
  (they share the request-scoped DI session; SQLite cannot handle
   concurrent writes on the same session)
"""


from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from dataclasses import dataclass

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import ExecutionState, SourceType
from .execution_service import ExecutionService
from .execution_store import JiraProjectRepositoryRecord
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




def compute_jira_event_id(payload: dict, repo: str | None = None) -> str:
    """Compute a deterministic event ID for idempotency.


    Combines the webhook event type, issue ID, and timestamp.
    When processing multiple repos for the same issue, a repo suffix
    is appended so each repo gets its own unique event ID.
    """
    webhook_event = payload.get('webhookEvent', '')
    issue_id = payload.get('issue', {}).get('id', '')
    timestamp = payload.get('timestamp', 0)
    raw = f'{webhook_event}:{issue_id}:{timestamp}'
    if repo:
        raw = f'{raw}:{repo}'
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


    Resolves repositories from the ``jira_project_repositories`` table
    (one per row for the same project key).  For each repo a separate
    execution is created **in parallel** (phase 1), then conversations
    are created **sequentially** (phase 2) to avoid SQLite transaction
    conflicts on the request-scoped DI session.
    """


    execution_service: ExecutionService
    openhands_client: OpenHandsClient


    # ── Phase 1 (parallel-safe) ──────────────────────────────────


    async def _create_execution_for_repo(
        self,
        *,
        payload: dict,
        issue_data: dict,
        repo_record: JiraProjectRepositoryRecord,
    ) -> dict | None:
        """Create execution record for one repo (parallel-safe).

        Each call uses its own DB session via ``ExecutionStore`` so
        multiple calls can run concurrently via ``asyncio.gather``.

        Returns a result dict with execution metadata, or ``None``
        when the event is a duplicate.
        """
        issue_key = issue_data['issue_key']
        summary = issue_data['summary']
        repo_str = f'{repo_record.owner}/{repo_record.repository}'
        default_branch = repo_record.default_branch or 'main'

        event_id = compute_jira_event_id(payload, repo=repo_str)
        branch = generate_jira_branch_name(
            issue_key, issue_data['issue_type'], summary
        )

        execution_record, is_new = await self.execution_service.create_execution(
            source_type=SourceType.JIRA,
            source_event_id=event_id,
            jira_issue_key=issue_key,
            branch=branch,
            repository=repo_str,
        )

        if not is_new:
            logger.info(
                f'[Automation] Duplicate event for {repo_str} '
                f'(execution {execution_record.execution_id})',
            )
            return None

        await self.execution_service.transition_state(
            execution_record.execution_id, ExecutionState.QUEUED
        )

        return {
            'execution_record': execution_record,
            'repo_str': repo_str,
            'default_branch': default_branch,
            'branch': branch,
        }


    # ── Phase 2 (sequential — shares request DI session) ─────────


    async def _create_conversation_for_execution(
        self,
        *,
        state,
        request,
        issue_data: dict,
        exec_data: dict,
        base_url: str,
    ) -> dict:
        """Create an OpenHands conversation for one execution.

        Must be called **sequentially** — it shares the request-scoped
        DI session which cannot handle concurrent writes on SQLite.
        """
        issue_key = issue_data['issue_key']
        summary = issue_data['summary']
        execution_record = exec_data['execution_record']
        repo_str = exec_data['repo_str']
        default_branch = exec_data['default_branch']
        branch = exec_data['branch']
        execution_id = execution_record.execution_id

        comment_endpoint = f'{base_url}/api/v1/jira/start/comment'

        prompt = render_prompt(
            'jira_new_conversation.j2',
            issue_key=issue_key,
            title=summary,
            issue_type=issue_data['issue_type'],
            priority=issue_data['priority'],
            reporter=issue_data['reporter'],
            description=issue_data['description'],
            repository=repo_str,
            default_branch=default_branch,
            branch=branch,
            comment_endpoint=comment_endpoint,
        )

        conversation_id = await self.openhands_client.create_conversation(
            state=state,
            request=request,
            prompt=prompt,
            title=f'[Automation] Jira {issue_key}',
            execution_id=execution_id,
            jira_issue_key=issue_key,
            repository=repo_str,
            branch=default_branch,
        )

        if conversation_id:
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
                'repository': repo_str,
            }

        await self.execution_service.transition_state(
            execution_id,
            ExecutionState.FAILED,
            error_message='Failed to create OpenHands conversation',
        )
        return {
            'status': 'failed',
            'execution_id': execution_id,
            'issue_key': issue_key,
            'repository': repo_str,
            'error': 'Failed to create conversation',
        }


    # ── Entry point ──────────────────────────────────────────────


    async def process_issue_created(
        self,
        payload: dict,
        state,
        request=None,
    ) -> dict:
        """Process a ``jira:issue_created`` webhook event.

        **Phase 1** — Create execution records **in parallel**
        (each uses its own DB session).
        **Phase 2** — Create conversations **sequentially**
        (they share the request-scoped DI session).

        If the project has a single repo configured the return value is a
        single-result dict (backward compatible).  With multiple repos the
        return value is ``{'status': 'multi', 'executions': [...]}``.
        """
        issue_data = extract_jira_issue_data(payload)
        if not issue_data:
            logger.warning('[Automation] Jira webhook: missing issue key')
            return {
                'status': 'skipped',
                'reason': 'Missing issue key in payload',
            }

        issue_key = issue_data['issue_key']
        project_key = extract_jira_project_key(payload)

        if not project_key:
            logger.error(
                '[Automation] Jira webhook: missing project key in '
                f'issue {issue_key}',
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

        # ── Phase 1: create executions in parallel ───────────────
        exec_tasks = [
            self._create_execution_for_repo(
                payload=payload,
                issue_data=issue_data,
                repo_record=repo,
            )
            for repo in repo_records
        ]
        exec_results: list[dict] = [
            r for r in await asyncio.gather(*exec_tasks) if r is not None
        ]

        if not exec_results:
            logger.info(
                f'[Automation] All events for {issue_key} were duplicates'
            )
            return {
                'status': 'duplicate',
                'issue_key': issue_key,
                'executions': [],
            }

        # ── Phase 2: create conversations sequentially ───────────
        base_url = str(request.base_url).rstrip('/')
        conv_results: list[dict] = []
        for exec_data in exec_results:
            result = await self._create_conversation_for_execution(
                state=state,
                request=request,
                issue_data=issue_data,
                exec_data=exec_data,
                base_url=base_url,
            )
            conv_results.append(result)

        # Single repo → backward-compatible single result
        if len(conv_results) == 1:
            return conv_results[0]

        # Multiple repos → aggregated response
        return {'status': 'multi', 'executions': conv_results}
