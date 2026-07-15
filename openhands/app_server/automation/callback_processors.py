"""Callback processors for execution lifecycle events.

Handles all post-execution operations when a conversation reaches a
terminal state — git commit/push, PR creation, Jira transitions, and
token usage reporting — using the deterministic service layer, not
LLM prompts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

from openhands.app_server.event_callback.event_callback_models import (
    EventCallback,
    EventCallbackProcessor,
    EventCallbackStatus,
    EventKind,
)
from openhands.app_server.event_callback.event_callback_result_models import (
    EventCallbackResult,
    EventCallbackResultStatus,
)
from openhands.app_server.utils.logger import openhands_logger as logger
from openhands.sdk import Event
from openhands.sdk.conversation import ConversationExecutionStatus
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent

from .correlation import build_log_context
from .execution_models import ExecutionRecord, ExecutionState, SourceType
from .execution_store import ExecutionStore

if TYPE_CHECKING:
    from fastapi import Request


class AutomationEventCallbackProcessor(EventCallbackProcessor):
    """Event callback processor for automation executions.

    Registered on automation-triggered conversations. Listens for
    ConversationStateUpdateEvent with terminal execution_status values
    (FINISHED, ERROR, STUCK), updates the execution record, runs
    deterministic post-execution operations via the service layer
    (git commit/push, PR creation, Jira transition + token comment,
    GitHub PR comment), and pauses the sandbox.

    When state and request are injected via set_request_context(),
    the processor can access sandbox info (agent_server_url,
    session_api_key) needed by the service layer.
    """

    event_kind: ClassVar[EventKind] = 'ConversationStateUpdateEvent'

    _state: object | None = None
    _request: 'Request | None' = None

    # Sandbox project directory — set once the sandbox is resolved
    _project_dir: str = '/workspace/project'

    def set_request_context(self, state: object, request: 'Request') -> None:
        """Store the request context for sandbox access on terminal state."""
        self._state = state
        self._request = request

    async def __call__(
        self,
        conversation_id: UUID,
        callback: EventCallback,
        event: Event,
    ) -> EventCallbackResult | None:
        if not isinstance(event, ConversationStateUpdateEvent):
            return None

        if event.key != 'execution_status':
            return None

        try:
            exec_status = ConversationExecutionStatus(event.value)
        except (ValueError, TypeError):
            return None

        if not exec_status.is_terminal():
            return None

        # ── Look up execution record ──────────────────────────────────
        store = ExecutionStore()
        record = await store.get_execution_by_conversation_id(
            str(conversation_id)
        )
        if not record:
            logger.info(
                '[Automation] No execution record found for '
                f'conversation {conversation_id} (may not be automation)'
            )
            return None

        # ── Determine execution state ─────────────────────────────────
        if exec_status == ConversationExecutionStatus.FINISHED:
            new_state = ExecutionState.COMPLETED
        else:
            new_state = ExecutionState.FAILED

        logger.info(
            f'[Automation] Conversation {conversation_id} reached '
            f'terminal state: {exec_status.value} (execution: '
            f'{record.execution_id} -> {new_state.value})',
            extra=build_log_context(
                execution_id=record.execution_id,
                conversation_id=str(conversation_id),
                jira_issue_key=record.jira_issue_key or '',
                pr_number=record.github_pr_id or 0,
                repository=record.repository or '',
            ),
        )

        # ── Run deterministic post-execution operations ──────────────
        if (
            self._state is not None
            and self._request is not None
            and exec_status == ConversationExecutionStatus.FINISHED
        ):
            await self._run_post_execution(record, conversation_id)

        # ── Update execution record ──────────────────────────────────
        await store.update_state(
            execution_id=record.execution_id,
            state=new_state,
            conversation_id=str(conversation_id),
        )

        # ── Pause the sandbox ────────────────────────────────────────
        if self._state is not None and self._request is not None:
            await self._pause_sandbox(conversation_id)

        # ── Disable this callback ───────────────────────────────────
        callback.status = EventCallbackStatus.COMPLETED

        return EventCallbackResult(
            status=EventCallbackResultStatus.SUCCESS,
            event_callback_id=callback.id,
            event_id=event.id,
            conversation_id=conversation_id,
        )

    # ── Post-execution orchestration ─────────────────────────────────

    async def _run_post_execution(
        self,
        record: 'ExecutionRecord',
        conversation_id: UUID,
    ) -> None:
        """Dispatch to the correct post-execution handler by source type."""
        agent_server_url, session_api_key = (
            await self._resolve_sandbox_info(conversation_id)
        )
        if not agent_server_url or not session_api_key:
            logger.warning(
                '[Automation] Cannot run post-execution: no sandbox info '
                f'for conversation {conversation_id}'
            )
            return

        source = record.source_type or ''
        if source == SourceType.JIRA.value:
            await self._handle_jira_post(
                record, agent_server_url, session_api_key,
                str(conversation_id),
            )
        elif source == SourceType.GITHUB.value:
            await self._handle_github_post(
                record, agent_server_url, session_api_key,
            )
        else:
            logger.info(
                f'[Automation] No post-execution handler for '
                f'source_type={source}'
            )

    async def _resolve_sandbox_info(
        self, conversation_id: UUID,
    ) -> tuple[str | None, str | None]:
        """Resolve agent_server_url and session_api_key from the sandbox."""
        try:
            from openhands.app_server.automation.github_webhook_router import (
                _get_agent_url_from_sandbox,
            )
            from openhands.app_server.config import (
                get_app_conversation_info_service,
                get_sandbox_service,
            )
            from openhands.app_server.utils.docker_utils import (
                replace_localhost_hostname_for_docker,
            )

            async with get_app_conversation_info_service(
                self._state, self._request
            ) as info_service:
                info = await info_service.get_app_conversation_info(
                    conversation_id
                )
                if not info or not info.sandbox_id:
                    return None, None

            async with get_sandbox_service(
                self._state, self._request
            ) as sandbox_service:
                sandbox = await sandbox_service.get_sandbox(info.sandbox_id)
                if not sandbox:
                    return None, None

                agent_url = _get_agent_url_from_sandbox(sandbox)
                if not agent_url:
                    return None, None

                agent_url = replace_localhost_hostname_for_docker(agent_url)
                return agent_url, sandbox.session_api_key

        except Exception:
            logger.error(
                '[Automation] Failed to resolve sandbox info for '
                f'conversation {conversation_id}:',
                exc_info=True,
            )
            return None, None

    # ── Jira post-execution ─────────────────────────────────────────

    async def _handle_jira_post(
        self,
        record: 'ExecutionRecord',
        agent_server_url: str,
        session_api_key: str,
        conversation_id_str: str,
    ) -> None:
        """Commit changes, push, create PR, transition Jira, post tokens."""
        jira_key = record.jira_issue_key
        if not jira_key:
            return

        # 1. Git commit and push
        pr_number: int | None = None
        pr_url: str | None = None

        try:
            from .services.sandbox_git_service import SandboxGitService

            git = SandboxGitService(
                agent_server_url, session_api_key, self._project_dir,
            )

            has_changes = await git.has_changes()
            if has_changes:
                commit_hash = await git.commit_all(
                    f'[Automation] {jira_key}: code changes from OpenHands',
                )
                await git.push(record.branch or 'main')
                logger.info(
                    f'[Automation] Pushed {commit_hash} to '
                    f'{record.branch} for {jira_key}',
                )

                # 2. Create PR
                if record.repository:
                    try:
                        from .services.github_api_service import (
                            GitHubApiService,
                        )

                        gh = GitHubApiService()
                        pr = await gh.create_pull_request(
                            repo=record.repository,
                            title=f'[Automation] {jira_key} — code changes',
                            body=(
                                f'Automated changes generated by OpenHands '
                                f'for Jira issue [{jira_key}].'
                            ),
                            head=record.branch or 'main',
                            base='main',
                        )
                        pr_number = pr.get('number')
                        pr_url = pr.get('html_url')
                        logger.info(
                            f'[Automation] Created PR #{pr_number} for '
                            f'{jira_key}: {pr_url}',
                        )
                    except Exception:
                        logger.error(
                            f'[Automation] Failed to create PR for '
                            f'{jira_key}:',
                            exc_info=True,
                        )
            else:
                logger.info(
                    f'[Automation] No changes to commit for {jira_key}',
                )
        except Exception:
            logger.error(
                f'[Automation] Git operations failed for {jira_key}:',
                exc_info=True,
            )

        # 3. Transition Jira issue
        try:
            from .services.jira_api_service import JiraApiService

            jira = JiraApiService()
            jira.transition_issue(jira_key, 'In Review')
            logger.info(
                f'[Automation] Transitioned {jira_key} to In Review',
            )
        except Exception:
            logger.warning(
                f'[Automation] Failed to transition {jira_key}:',
                exc_info=True,
            )

        # 4. Fetch metrics and post token usage comment
        try:
            from .services.metrics_service import MetricsService

            metrics = MetricsService()
            m = await metrics.fetch_live_metrics(
                agent_server_url, conversation_id_str, session_api_key,
            )
            if m:
                comment_adf = metrics.build_token_usage_comment(**m)
                jira_client = JiraApiService()
                jira_client.add_or_update_token_usage_comment(
                    jira_key, comment_adf,
                )
                logger.info(
                    f'[Automation] Posted token usage to {jira_key}',
                )
        except Exception:
            logger.warning(
                f'[Automation] Failed to post token usage for {jira_key}:',
                exc_info=True,
            )

    # ── GitHub post-execution ───────────────────────────────────────

    async def _handle_github_post(
        self,
        record: 'ExecutionRecord',
        agent_server_url: str,
        session_api_key: str,
    ) -> None:
        """Commit changes, push, and update PR comment for GitHub flows."""
        repo = record.repository
        pr_number = record.github_pr_id
        branch = record.branch or 'main'

        if not repo or not pr_number:
            return

        # 1. Git commit and push
        try:
            from .services.sandbox_git_service import SandboxGitService

            git = SandboxGitService(
                agent_server_url, session_api_key, self._project_dir,
            )

            has_changes = await git.has_changes()
            if has_changes:
                commit_hash = await git.commit_all(
                    f'[Automation] PR #{pr_number}: code review changes',
                )
                await git.push(branch)
                logger.info(
                    f'[Automation] Pushed {commit_hash} to {branch} '
                    f'for PR #{pr_number}',
                )
            else:
                logger.info(
                    f'[Automation] No changes to push for PR #{pr_number}',
                )
        except Exception:
            logger.error(
                f'[Automation] Git operations failed for PR #{pr_number}:',
                exc_info=True,
            )

        # 2. Add PR comment with completion summary
        try:
            from .services.github_api_service import GitHubApiService

            gh = GitHubApiService()
            comment = (
                f'✅ OpenHands review complete.\n\n'
                f'Changes have been committed and pushed to '
                f'`{branch}`.'
            )
            await gh.add_pr_comment(repo, pr_number, comment)
            logger.info(
                f'[Automation] Posted completion comment on PR #{pr_number}',
            )
        except Exception:
            logger.error(
                f'[Automation] Failed to comment on PR #{pr_number}:',
                exc_info=True,
            )

    # ── Sandbox lifecycle ───────────────────────────────────────────

    async def _pause_sandbox(self, conversation_id: UUID) -> None:
        """Pause the sandbox associated with this conversation."""
        try:
            from openhands.app_server.config import (
                get_app_conversation_info_service,
            )
            from openhands.app_server.utils.sandbox_utils import (
                pause_sandbox,
            )

            async with get_app_conversation_info_service(
                self._state, self._request
            ) as info_service:
                info = await info_service.get_app_conversation_info(
                    conversation_id
                )
                if info and info.sandbox_id:
                    await pause_sandbox(
                        info.sandbox_id, self._state, self._request
                    )
        except Exception:
            logger.error(
                '[Automation] Failed to pause sandbox for conversation '
                f'{conversation_id}:',
                exc_info=True,
            )


