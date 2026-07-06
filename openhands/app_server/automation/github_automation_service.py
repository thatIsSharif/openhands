"""
GitHub automation service - processes PR review webhooks.

Handles:
- Webhook signature verification (HMAC-SHA256)
- Event ID computation for idempotency
- PR review data extraction (review comments and submitted reviews)
- Execution and conversation creation (NEW conversation per review)
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from openhands.app_server.utils.github import add_pr_comment
from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import ExecutionState, SourceType
from .execution_service import ExecutionService
from .input_sanitizer import (
    build_rejection_message,
    has_dangerous_patterns,
)
from .openhands_client import OpenHandsClient
from .prompt_renderer import render_prompt

GITHUB_WEBHOOK_EVENTS = frozenset(
    {
        'pull_request_review_comment',
        'pull_request_review',
        'pull_request',
        'issue_comment',
    }
)


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

    received = signature_header[len(prefix) :]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def compute_github_event_id(
    payload: dict,
    delivery_id: str | None,
    event_type: str = 'pull_request_review_comment',
) -> str:
    """Compute a deterministic event ID for idempotency.

    Uses the delivery ID combined with the relevant object ID
    (comment ID for review comments, review ID for submitted reviews).
    """
    if event_type == 'pull_request_review':
        obj_id = payload.get('review', {}).get('id', '')
    else:
        obj_id = payload.get('comment', {}).get('id', '')
    raw = f'{delivery_id or ""}:{obj_id}'
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


def extract_github_review_submitted_data(
    payload: dict,
) -> dict | None:
    """Extract PR review data from a pull_request_review webhook payload.

    The pull_request_review payload has a different structure from
    pull_request_review_comment — the review object is at the top level
    instead of comment, and includes a review state (approved, changes_requested,
    comment) and an action field (submitted, edited, dismissed).

    Returns dict with keys: repository, owner, pr_number, branch,
    pr_title, pr_body, review_comment, reviewer, review_id, review_state.

    Returns None if required fields are missing or action is not 'submitted'.
    """
    action = payload.get('action', '')
    if action != 'submitted':
        return None

    repo_data = payload.get('repository', {})
    pr_data = payload.get('pull_request', {})
    review_data = payload.get('review', {}) or {}
    sender_data = payload.get('sender', {})

    full_name = repo_data.get('full_name', '')
    owner = (repo_data.get('owner', {}) or {}).get('login', '')
    pr_number = pr_data.get('number')
    branch = (pr_data.get('head', {}) or {}).get('ref', '')
    pr_title = pr_data.get('title', '')
    pr_body = pr_data.get('body') or ''
    review_comment = review_data.get('body', '') or ''
    reviewer = sender_data.get('login', '')
    review_id = review_data.get('id')
    review_state = review_data.get('state', '')

    if not full_name or not pr_number:
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
        'review_id': review_id,
        'review_state': review_state,
    }


@dataclass
class GitHubAutomationService:
    """Processes GitHub PR review webhook events.

    Flow:
    1. Verify webhook signature
    2. Compute event ID for idempotency
    3. Extract PR review data
    4. Create execution record (NEW conversation per review)
    5. Create NEW OpenHands conversation with repository attached
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
            logger.warning('[Automation] GitHub webhook: missing required fields')
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
        event_id = compute_github_event_id(
            payload, delivery_id, event_type='pull_request_review_comment'
        )

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
            execution_id, ExecutionState.QUEUED
        )

        # Build the comment endpoint URL from the incoming request
        base_url = str(request.base_url).rstrip('/')
        comment_endpoint = f'{base_url}/api/v1/git/github/webhook/comment'

        # ── Input sanitization (Layer 1) ────────────────────────────
        # Check the review comment (the main user-controlled text field)
        is_dangerous, labels = has_dangerous_patterns(
            review_comment, field_name='github_review_comment'
        )
        if is_dangerous:
            logger.warning(
                '[Security] Rejecting GitHub review comment on %s PR #%d '
                'by %s: dangerous patterns=%s',
                repository, pr_number, reviewer, labels,
            )
            add_pr_comment(repository, pr_number, build_rejection_message(review_comment))
            await self.execution_service.transition_state(
                execution_id,
                ExecutionState.FAILED,
                error_message=f'Review comment rejected: dangerous patterns ({", ".join(labels)})',
            )
            return {
                'status': 'rejected',
                'execution_id': execution_id,
                'pr_number': pr_number,
                'repository': repository,
                'reason': 'Review comment contains dangerous patterns',
            }

        # Build prompt for the agent
        prompt = render_prompt(
            'github_review_conversation.j2',
            pr_number=pr_number,
            repository=repository,
            reviewer=reviewer,
            review_comment=review_comment,
            branch=branch,
            comment_endpoint=comment_endpoint,
        )

        # Create NEW OpenHands conversation with repository attached
        conversation_id = await self.openhands_client.create_conversation(
            state=state,
            request=request,
            prompt=prompt,
            title=f'[Automation] GitHub PR #{pr_number} Review',
            execution_id=execution_id,
            pr_number=pr_number,
            repository=repository,
            branch=branch,
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
                'pr_number': pr_number,
                'repository': repository,
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
                'pr_number': pr_number,
                'error': 'Failed to create conversation',
            }

    async def process_review_submitted(
        self,
        payload: dict,
        state,
        request=None,
        delivery_id: str | None = None,
    ) -> dict:
        """Process a pull_request_review (submitted) webhook event.

        Creates a NEW OpenHands conversation when a full review
        is submitted (approved, changes_requested, or comment).
        Never reuses old conversations.

        Idempotency is provided via the X-GitHub-Delivery header
        which is passed as the source_event_id.

        Returns a dict for the webhook response.
        """
        # Extract PR review submitted data
        review_data = extract_github_review_submitted_data(payload)
        if not review_data:
            logger.warning(
                '[Automation] GitHub pull_request_review: missing '
                'required fields or action is not submitted'
            )
            return {
                'status': 'skipped',
                'reason': 'Missing required fields or action is not submitted',
            }

        repository = review_data['repository']
        pr_number = review_data['pr_number']
        branch = review_data['branch']
        review_comment = review_data['review_comment']
        reviewer = review_data['reviewer']
        review_state = review_data['review_state']

        logger.info(
            f'[Automation] Processing review ({review_state}) on '
            f'{repository} PR #{pr_number} by {reviewer}',
            extra=build_log_context(
                execution_id='',
                repository=repository,
                branch=branch,
                pr_number=pr_number,
            ),
        )

        # Use X-GitHub-Delivery as source_event_id for idempotency
        event_id = compute_github_event_id(
            payload, delivery_id, event_type='pull_request_review'
        )

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
            execution_id, ExecutionState.QUEUED
        )

        # Build the comment endpoint URL from the incoming request
        base_url = str(request.base_url).rstrip('/')
        comment_endpoint = f'{base_url}/api/v1/git/github/webhook/comment'

        # ── Input sanitization (Layer 1) ────────────────────────────
        # Check the review comment (the main user-controlled text field)
        is_dangerous, labels = has_dangerous_patterns(
            review_comment, field_name='github_review_submitted_comment'
        )
        if is_dangerous:
            logger.warning(
                '[Security] Rejecting GitHub review submitted on %s PR #%d '
                'by %s: dangerous patterns=%s',
                repository, pr_number, reviewer, labels,
            )
            add_pr_comment(repository, pr_number, build_rejection_message(review_comment))
            await self.execution_service.transition_state(
                execution_id,
                ExecutionState.FAILED,
                error_message=(
                    'Review submitted comment rejected: '
                    f'dangerous patterns ({", ".join(labels)})'
                ),
            )
            return {
                'status': 'rejected',
                'execution_id': execution_id,
                'pr_number': pr_number,
                'repository': repository,
                'reason': 'Review submitted comment contains dangerous patterns',
            }

        # Build prompt for the agent
        state_desc = {
            'approved': 'approved',
            'changes_requested': 'requested changes',
            'comment': 'left a comment',
        }.get(review_state, f'({review_state})')
        prompt = render_prompt(
            'github_review_submitted_conversation.j2',
            pr_number=pr_number,
            repository=repository,
            reviewer=reviewer,
            review_state=review_state,
            review_comment=review_comment,
            branch=branch,
            comment_endpoint=comment_endpoint,
        )

        # Create NEW OpenHands conversation with repository attached
        conversation_id = await self.openhands_client.create_conversation(
            state=state,
            request=request,
            prompt=prompt,
            title=f'[Automation] GitHub PR #{pr_number} {state_desc.title()}',
            execution_id=execution_id,
            pr_number=pr_number,
            repository=repository,
            branch=branch,
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
                'pr_number': pr_number,
                'repository': repository,
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
                'pr_number': pr_number,
                'error': 'Failed to create conversation',
            }
