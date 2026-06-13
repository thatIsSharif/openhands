"""GitHub automation service - processes PR review comment webhooks and creates executions.

This is a standalone service that does not extend the existing GithubManager,
keeping the automation platform concerns separate from the label-triggered resolver.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import ExecutionState, SourceType
from .execution_service import ExecutionService
from .execution_store import ExecutionStore
from .openhands_client import OpenHandsClient


def verify_github_signature(
    payload_body: bytes,
    signature_header: str | None,
    webhook_secret: str,
) -> bool:
    """Verify HMAC-SHA256 signature for GitHub webhook requests.

    GitHub uses the X-Hub-Signature-256 header with HMAC-SHA256.
    """
    if not signature_header:
        return False
    try:
        # Expected format: sha256=<hex-digest>
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


def compute_github_event_id(payload: dict, delivery_id: str) -> str:
    """Compute a unique event ID for GitHub webhook idempotency.

    Uses the X-GitHub-Delivery header combined with the comment ID.
    """
    comment = payload.get('comment', {})
    comment_id = comment.get('id', '')
    raw = f'github:{delivery_id}:{comment_id}'
    return hashlib.sha256(raw.encode()).hexdigest()


def extract_github_review_data(
    payload: dict,
) -> dict | None:
    """Extract normalized PR review comment data from a GitHub webhook payload.

    Returns a dict with repository, owner, pr_number, branch, title,
    review_comment, reviewer, comment_id, or None if required fields missing.
    """
    repo = payload.get('repository', {})
    pr = payload.get('pull_request', {})
    comment = payload.get('comment', {})
    sender = payload.get('sender', {})

    full_name = repo.get('full_name', '')
    owner = repo.get('owner', {}).get('login', '')
    pr_number = pr.get('number')
    branch = pr.get('head', {}).get('ref', '')
    pr_title = pr.get('title', '')
    pr_body = pr.get('body', '')
    comment_body = comment.get('body', '')
    reviewer = sender.get('login', '')
    comment_id = comment.get('id')

    if not full_name or not pr_number or not comment_body:
        logger.warning(
            '[GitHubAutomation] Missing required fields in review payload'
        )
        return None

    return {
        'repository': full_name,
        'owner': owner,
        'pr_number': pr_number,
        'branch': branch,
        'pr_title': pr_title,
        'pr_body': pr_body or '',
        'review_comment': comment_body,
        'reviewer': reviewer,
        'comment_id': comment_id,
    }


class GitHubAutomationService:
    """Processes GitHub PR review comment webhooks and manages the automation workflow."""

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

    async def handle_review_comment(
        self,
        payload: dict,
        delivery_id: str,
        webhook_secret: str | None = None,
        signature: str | None = None,
    ) -> dict:
        """Handle a pull_request_review_comment webhook event.

        Steps:
        1. Verify signature (if secret configured)
        2. Compute event ID for idempotency
        3. Extract review data
        4. Create execution record
        5. Queue execution as background task

        Returns:
            Response dict with execution_id and status.
        """
        # Signature verification
        if webhook_secret and signature:
            raw_body = json.dumps(payload).encode()
            if not verify_github_signature(raw_body, signature, webhook_secret):
                logger.warning('[GitHubAutomation] Invalid webhook signature')
                return {
                    'error': 'Invalid webhook signature',
                    'status': 'REJECTED',
                }

        # Compute idempotency key
        event_id = compute_github_event_id(payload, delivery_id)

        # Extract review data
        review_data = extract_github_review_data(payload)
        if not review_data:
            return {
                'error': 'Failed to extract review data from payload',
                'status': 'REJECTED',
            }

        log_ctx = build_log_context(
            repository=review_data['repository'],
            branch=review_data['branch'],
            pr_number=review_data['pr_number'],
        )
        logger.info(
            f'[GitHubAutomation] Processing review comment '
            f'on PR #{review_data["pr_number"]}',
            extra=log_ctx,
        )

        # Check if the action is 'created' (new review comment)
        action = payload.get('action', '')
        if action != 'created':
            return {
                'status': 'SKIPPED',
                'message': f'Unhandled action: {action}',
            }

        # Create execution with idempotency check
        record, is_new = await self._execution_service.create_execution(
            source_type=SourceType.GITHUB,
            source_event_id=event_id,
            github_pr_id=review_data['pr_number'],
            repository=review_data['repository'],
            branch=review_data['branch'],
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

        # Upsert GitHub PR metadata
        await self._execution_store.upsert_github_pull_request(
            pr_number=review_data['pr_number'],
            repository=review_data['repository'],
            owner=review_data['owner'],
            branch=review_data['branch'],
            title=review_data['pr_title'],
            execution_id=record.id,
        )

        # Create review iteration record
        await self._execution_store.create_review_iteration(
            execution_id=record.id if record.id else 0,
            iteration_number=1,
            review_comment_id=review_data['comment_id'],
            reviewer=review_data['reviewer'],
            comment_body=review_data['review_comment'],
            pr_number=review_data['pr_number'],
            repository=review_data['repository'],
        )

        return {
            'execution_id': record.execution_id,
            'status': 'RECEIVED',
            'message': 'Review comment received, execution queued',
        }

    async def execute_review_comment(
        self,
        execution_id: str,
        repository: str,
        pr_number: int,
        branch: str,
        pr_title: str,
        pr_body: str,
        review_comment: str,
        reviewer: str,
    ) -> None:
        """Execute a PR review comment automation in the background.

        This is called by the background task after the webhook returns.
        """
        log_ctx = build_log_context(
            execution_id=execution_id,
            repository=repository,
            branch=branch,
            pr_number=pr_number,
        )

        try:
            # Transition to RUNNING
            await self._execution_service.transition_state(
                execution_id, ExecutionState.RUNNING
            )

            # Render template
            jinja_env = self._openhands_client.get_template_env()
            template = jinja_env.get_template(
                'automation/github_review_conversation.j2'
            )
            prompt = template.render(
                repository=repository,
                pr_number=pr_number,
                pr_title=pr_title,
                pr_description=pr_body,
                branch_name=branch,
                review_comment=review_comment,
                reviewer=reviewer,
                unresolved_reviews=[],
            )

            # Create NEW OpenHands conversation (do not reuse old ones)
            conversation_id = await self._openhands_client.create_conversation(
                execution_id=execution_id,
                prompt=prompt,
                repository=repository,
                branch=branch,
                pr_number=pr_number,
            )

            if conversation_id:
                # Update execution with conversation ID
                await self._execution_service.transition_state(
                    execution_id,
                    ExecutionState.RUNNING,
                    conversation_id=conversation_id,
                )
                logger.info(
                    f'[GitHubAutomation] Started execution {execution_id} '
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
                f'[GitHubAutomation] Execution {execution_id} failed: {e}',
                extra=log_ctx,
            )
            await self._execution_service.transition_state(
                execution_id,
                ExecutionState.FAILED,
                error_message=str(e),
            )
