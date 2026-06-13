"""GitHub automation service - processes PR review comment webhooks.

Handles:
- Webhook signature verification (HMAC-SHA256)
- Event ID computation for idempotency
- PR review comment data extraction
- Execution and conversation creation (NEW conversation per review)
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import SourceType
from .execution_service import ExecutionService
from .openhands_client import OpenHandsClient

GITHUB_WEBHOOK_EVENTS = frozenset({
    'pull_request_review_comment',
    'pull_request_review',
    'pull_request',
    'issue_comment',
})


def verify_github_signature(
    body: bytes, signature_header: str | None, secret: str
) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    GitHub sends signatures in the format: sha256=<hex_digest>
    """
    if not signature_header:
        return False

    prefix = 'sha256='
    if not signature_header.startswith(prefix):
        return False

    received = signature_header[len(prefix):]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def compute_github_event_id(payload: dict, delivery_id: str | None) -> str:
    """Compute a deterministic event ID for idempotency.

    Combines the delivery ID with the comment ID.
    """
    comment_id = payload.get('comment', {}).get('id', '')
    raw = f'{delivery_id or ""}:{comment_id}'
    return hashlib.sha256(raw.encode()).hexdigest()


def extract_github_review_data(
    payload: dict,
) -> dict | None:
    """Extract PR review comment data from a GitHub webhook payload.

    Returns dict with keys: repository, owner, pr_number, branch,
    pr_title, pr_body, review_comment, reviewer, comment_id.

    Returns None if required fields are missing.
    """
    repo_data = payload.get('repository', {})
    pr_data = payload.get('pull_request', {})
    comment_data = payload.get('comment', {})
    sender_data = payload.get('sender', {})

    full_name = repo_data.get('full_name', '')
    owner = (repo_data.get('owner', {}) or {}).get('login', '')
    pr_number = pr_data.get('number')
    branch = (pr_data.get('head', {}) or {}).get('ref', '')
    pr_title = pr_data.get('title', '')
    pr_body = pr_data.get('body') or ''
    review_comment = comment_data.get('body', '')
    reviewer = sender_data.get('login', '')
    comment_id = comment_data.get('id')

    if not full_name or not pr_number or not review_comment:
        return None

    return {
        'repository': full_name,
        'owner': owner,
        'pr_number': pr_number,
        'branch': branch,
        'pr_title': pr_title,
        'pr_body': pr_body,
        'review_comment': review_comment,
        'reviewer': reviewer,
        'comment_id': comment_id,
    }


@dataclass
class GitHubAutomationService:
    """Processes GitHub PR review comment webhook events.

    Flow:
    1. Verify webhook signature
    2. Compute event ID for idempotency
    3. Extract PR review data
    4. Create execution record (NEW conversation per review)
    5. Fetch PR context (details, diff, unresolved comments)
    6. Create NEW OpenHands conversation
    """

    execution_service: ExecutionService
    openhands_client: OpenHandsClient

    async def process_review_comment(
        self,
        payload: dict,
        state,
        request=None,
        delivery_id: str | None = None,
    ) -> dict:
        """Process a pull_request_review_comment webhook event.

        Creates a NEW OpenHands conversation for each review cycle.
        Never reuses old conversations.

        Idempotency is provided via the X-GitHub-Delivery header
        which is passed as the source_event_id.

        Returns a dict for the webhook response.
        """
        # Extract PR review data
        review_data = extract_github_review_data(payload)
        if not review_data:
            logger.warning(
                '[Automation] GitHub webhook: missing required fields'
            )
            return {
                'status': 'skipped',
                'reason': 'Missing required fields in payload',
            }

        repository = review_data['repository']
        pr_number = review_data['pr_number']
        branch = review_data['branch']
        review_comment = review_data['review_comment']
        reviewer = review_data['reviewer']

        logger.info(
            f'[Automation] Processing review comment on {repository} '
            f'PR #{pr_number} by {reviewer}',
            extra=build_log_context(
                execution_id='',
                repository=repository,
                branch=branch,
                pr_number=pr_number,
            ),
        )

        # Use X-GitHub-Delivery as source_event_id for idempotency
        event_id = compute_github_event_id(payload, delivery_id)

        execution_record, is_new = await self.execution_service.create_execution(
            source_type=SourceType.GITHUB,
            source_event_id=event_id,
            jira_issue_key=None,
            github_pr_id=pr_number,
            repository=repository,
            branch=branch,
        )

        if not is_new:
            return {
                'status': 'duplicate',
                'execution_id': execution_record.execution_id,
            }

        execution_id = execution_record.execution_id

        # Transition to QUEUED
        await self.execution_service.transition_state(
            execution_id, 'QUEUED'  # type: ignore[arg-type]
        )

        # Build prompt for the agent
        prompt = (
            f'A review comment was posted on pull request #{pr_number} '
            f'in {repository}.\n\n'
            f'Reviewer: {reviewer}\n'
            f'Comment: {review_comment}\n\n'
            f'Review branch: {branch}\n\n'
            f'Please:\n'
            f'1. Read the PR context and the review comment(s)\n'
            f'2. Determine the required fixes\n'
            f'3. Commit changes to the existing branch ({branch})\n'
            f'4. Update the existing pull request (#{pr_number})\n\n'
            f'The PR is the source of truth. Work directly on the '
            f'existing branch and PR.'
        )

        # Create NEW OpenHands conversation (never reuse old ones)
        conversation_id = await self.openhands_client.create_conversation(
            state=state,
            request=request,
            prompt=prompt,
            title=f'[Automation] GitHub PR #{pr_number} Review',
            execution_id=execution_id,
            pr_number=pr_number,
            repository=repository,
        )

        if conversation_id:
            await self.execution_service.transition_state(
                execution_id,
                'RUNNING',  # type: ignore[arg-type]
                conversation_id=conversation_id,
            )
            return {
                'status': 'running',
                'execution_id': execution_id,
                'conversation_id': conversation_id,
                'pr_number': pr_number,
                'repository': repository,
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
                'pr_number': pr_number,
                'error': 'Failed to create conversation',
            }
