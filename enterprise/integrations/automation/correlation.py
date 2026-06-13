"""Correlation ID utilities for execution traceability.

Every automation execution generates a unique execution_id that is propagated
through all layers: webhook ingestion, OpenHands conversation, GitHub operations,
Jira operations, Langfuse traces, and structured logs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def generate_execution_id() -> str:
    """Generate a unique execution correlation ID.

    Format: exec_<uuid_short> (e.g., exec_a1b2c3d4e5f6)
    """
    short_uuid = uuid.uuid4().hex[:12]
    return f'exec_{short_uuid}'


def generate_conversation_title(
    source_type: str,
    jira_issue_key: str | None = None,
    pr_number: int | None = None,
) -> str:
    """Generate a human-readable conversation title."""
    if jira_issue_key:
        return f'[Automation] Jira {jira_issue_key}'
    if pr_number:
        return f'[Automation] GitHub PR #{pr_number}'
    return f'[Automation] {source_type}'


def build_log_context(
    execution_id: str,
    conversation_id: str | None = None,
    repository: str | None = None,
    branch: str | None = None,
    jira_issue_key: str | None = None,
    pr_number: int | None = None,
) -> dict:
    """Build structured logging context with all correlation fields."""
    context: dict = {
        'execution_id': execution_id,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    if conversation_id:
        context['conversation_id'] = conversation_id
    if repository:
        context['repository'] = repository
    if branch:
        context['branch'] = branch
    if jira_issue_key:
        context['jira_issue_key'] = jira_issue_key
    if pr_number is not None:
        context['pr_number'] = pr_number
    return context
