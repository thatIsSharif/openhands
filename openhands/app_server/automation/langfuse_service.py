"""Optional Langfuse observability service for automation executions.

Provides structured tracing for every automation execution. The service
is a no-op when Langfuse is not configured, making it safe to use in
all deployments without requiring Langfuse infrastructure.

Trace Hierarchy (when configured):
Trace: exec_{execution_id}
├── Span: OpenHands Run
│   ├── Generation: LLM Calls (captured per-LLM-event)
│   ├── Span: Git Operations
│   │   ├── Span: Branch Creation
│   │   ├── Span: Commit Operations
│   │   └── Span: Pull Request Operations
│   └── Span: Tool Calls

Required environment variables to activate tracing:
- LANGFUSE_PUBLIC_KEY
- LANGFUSE_SECRET_KEY
- LANGFUSE_HOST (optional, defaults to https://cloud.langfuse.com)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context
from .execution_models import ExecutionRecord

# Sentinel for "not configured" state
_LANGFUSE_AVAILABLE: bool | None = None


def _is_langfuse_configured() -> bool:
    """Check if Langfuse environment variables are set."""
    global _LANGFUSE_AVAILABLE
    if _LANGFUSE_AVAILABLE is None:
        pk = os.environ.get('LANGFUSE_PUBLIC_KEY', '')
        sk = os.environ.get('LANGFUSE_SECRET_KEY', '')
        _LANGFUSE_AVAILABLE = bool(pk) and bool(sk)
    return _LANGFUSE_AVAILABLE


@dataclass
class LangfuseTraceContext:
    """Holds reference to an active Langfuse trace and spans."""

    trace_id: str
    execution_id: str


@dataclass
class LangfuseService:
    """Observability service that creates Langfuse traces for executions.

    The service is a no-op when Langfuse is not configured (i.e.,
    LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not both set).
    This makes it safe to use in all deployments.

    To activate: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY env vars.
    """

    # Lazy-imported Langfuse client
    _client: object | None = field(default=None, init=False, repr=False)

    def _get_client(self):
        """Get or create the Langfuse client (lazy init)."""
        if self._client is not None:
            return self._client
        if not _is_langfuse_configured():
            return None
        try:
            from langfuse import Langfuse as LangfuseClient

            host = os.environ.get(
                'LANGFUSE_HOST', 'https://cloud.langfuse.com'
            )
            self._client = LangfuseClient(
                public_key=os.environ['LANGFUSE_PUBLIC_KEY'],
                secret_key=os.environ['LANGFUSE_SECRET_KEY'],
                host=host,
            )
            logger.info('[Automation] Langfuse tracing enabled')
            return self._client
        except ImportError:
            logger.warning(
                '[Automation] Langfuse SDK not installed. '
                'Install with: pip install langfuse'
            )
            return None
        except Exception as e:
            logger.warning(
                f'[Automation] Failed to initialize Langfuse: {e}'
            )
            return None

    async def start_trace(
        self,
        execution: ExecutionRecord,
    ) -> LangfuseTraceContext | None:
        """Create a Langfuse trace for an automation execution.

        Returns a trace context if Langfuse is configured and the
        trace was created. Returns None if Langfuse is not available
        (no-op mode).
        """
        client = self._get_client()
        if client is None:
            return None

        trace_id = f'exec_{execution.execution_id}'

        try:
            # Build trace metadata
            metadata: dict = {
                'source_type': execution.source_type,
                'execution_id': execution.execution_id,
            }
            if execution.jira_issue_key:
                metadata['jira_issue_key'] = execution.jira_issue_key
            if execution.github_pr_id:
                metadata['github_pr_id'] = execution.github_pr_id
            if execution.repository:
                metadata['repository'] = execution.repository
            if execution.branch:
                metadata['branch'] = execution.branch

            trace = client.trace(
                id=trace_id,
                name=f'Automation: {execution.source_type}',
                input=metadata,
                metadata=metadata,
                session_id=execution.execution_id,
            )
            # Create root span
            trace.span(
                name='OpenHands Run',
                metadata=metadata,
            )

            logger.info(
                f'[Automation] Langfuse trace created: {trace_id}',
                extra=build_log_context(
                    execution_id=execution.execution_id,
                    jira_issue_key=execution.jira_issue_key,
                    pr_number=execution.github_pr_id,
                    repository=execution.repository,
                    branch=execution.branch,
                ),
            )
            return LangfuseTraceContext(
                trace_id=trace_id,
                execution_id=execution.execution_id,
            )
        except Exception as e:
            logger.warning(
                f'[Automation] Failed to create Langfuse trace: {e}'
            )
            return None

    async def finalize_trace(
        self,
        trace_ctx: LangfuseTraceContext | None,
        execution: ExecutionRecord,
    ) -> None:
        """Finalize a Langfuse trace with execution results.

        Updates the trace with output, final status, cost, and
        token usage. Safe to call with None trace_ctx (no-op).
        """
        if trace_ctx is None:
            return

        client = self._get_client()
        if client is None:
            return

        try:
            output: dict = {
                'status': execution.state.value,
                'execution_id': execution.execution_id,
            }
            if execution.conversation_id:
                output['conversation_id'] = execution.conversation_id
            if execution.error_message:
                output['error_message'] = execution.error_message
            if execution.started_at and execution.completed_at:
                duration = (
                    execution.completed_at - execution.started_at
                ).total_seconds()
                output['duration_seconds'] = duration

            client.trace(
                id=trace_ctx.trace_id,
                output=output,
                metadata={
                    **({'level': 'ERROR'} if execution.state.value == 'FAILED' else {}),
                },
            )

            logger.info(
                f'[Automation] Langfuse trace finalized: '
                f'{trace_ctx.trace_id} → {execution.state.value}',
                extra=build_log_context(
                    execution_id=execution.execution_id,
                    conversation_id=execution.conversation_id or '',
                ),
            )
        except Exception as e:
            logger.warning(
                f'[Automation] Failed to finalize Langfuse trace: {e}'
            )
