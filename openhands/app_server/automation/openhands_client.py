from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Request

from openhands.agent_server.models import (
    SendMessageRequest,
    TextContent,
)
from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversationStartRequest,
    AppConversationStartTaskStatus,
    ConversationTrigger,
)
from openhands.app_server.automation.budget_enforcement_processor import (
    BudgetEnforcementProcessor,
)
from openhands.app_server.automation.callback_processors import (
    AutomationEventCallbackProcessor,
)
from openhands.app_server.automation.execution_store import ExecutionStore
from openhands.app_server.config import (
    get_app_conversation_info_service,
    get_app_conversation_service,
    get_httpx_client,
    get_sandbox_service,
)
from openhands.app_server.integrations.service_types import ProviderType
from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)
from openhands.app_server.utils.logger import (
    openhands_logger as logger,
)

from .correlation import build_log_context


@dataclass
class OpenHandsClient:
    """Creates automation conversations through the OSS service layer."""

    async def create_conversation(
        self,
        state,
        request: Request | None,
        prompt: str,
        title: str = '[Automation] Execution',
        execution_id: str | None = None,
        jira_issue_key: str | None = None,
        pr_number: int | None = None,
        repository: str | None = None,
        branch: str | None = None,
    ) -> str | None:
        # Look up execution record for task-level rate limits
        max_iterations: int | None = None
        max_budget: float | None = None
        if execution_id:
            store = ExecutionStore()
            execution_record = await store.get_execution(execution_id)
            if execution_record:
                max_iterations = execution_record.max_iterations
                max_budget = execution_record.max_budget

        # Build the processor list
        processors = [AutomationEventCallbackProcessor()]

        # Register budget enforcement if a max_budget is configured
        if max_budget is not None and max_budget > 0:
            processors.append(BudgetEnforcementProcessor())

        start_request = AppConversationStartRequest(
            trigger=ConversationTrigger.AUTOMATION,
            title=title,
            selected_repository=repository,
            selected_branch=branch or 'main',
            git_provider=ProviderType.GITHUB,
            initial_message=SendMessageRequest(
                content=[
                    TextContent(
                        text=prompt,
                    )
                ]
            ),
            max_iterations=max_iterations,
            max_budget_per_task=max_budget,
            processors=processors,
            jira_issue_key=jira_issue_key,
        )

        async with get_app_conversation_service(
            state,
            request,
        ) as service:
            if service is None:
                logger.error(
                    '[Automation] AppConversationService not available',
                    extra=build_log_context(
                        execution_id=execution_id or '',
                        jira_issue_key=jira_issue_key,
                    ),
                )
                return None

            try:
                conversation_id = None

                async for task in service.start_app_conversation(start_request):
                    logger.info(f'[Automation] Start task status: {task.status}')

                    if task.status == AppConversationStartTaskStatus.READY:
                        conversation_id = str(task.app_conversation_id)
                        break

                    if task.status == AppConversationStartTaskStatus.ERROR:
                        logger.error(
                            f'[Automation] Conversation startup failed: {task.detail}'
                        )
                        return None

                if not conversation_id:
                    logger.error(
                        '[Automation] Conversation startup '
                        'finished without READY status'
                    )
                    return None

                logger.info(
                    f'[Automation] Created conversation {conversation_id}',
                    extra=build_log_context(
                        execution_id=execution_id or '',
                        conversation_id=conversation_id,
                        jira_issue_key=jira_issue_key,
                        pr_number=pr_number,
                        repository=repository,
                    ),
                )

                # ── Layer 2: Set automation security analyzer ─────────
                await self._set_automation_security_analyzer(
                    state=state,
                    request=request,
                    conversation_id=conversation_id,
                    execution_id=execution_id,
                    jira_issue_key=jira_issue_key,
                )

                return conversation_id

            except Exception:
                import traceback

                logger.error(traceback.format_exc())
                return None

    async def _set_automation_security_analyzer(
        self,
        state,
        request: Request | None,
        conversation_id: str,
        execution_id: str | None = None,
        jira_issue_key: str | None = None,
    ) -> None:
        """Set the automation security analyzer on a conversation.

        Wraps the AutomationSecurityAnalyzer together with the SDK's
        PatternSecurityAnalyzer and PolicyRailSecurityAnalyzer into
        an EnsembleSecurityAnalyzer and registers it on the agent server.

        This is applied after conversation creation so the security
        guardrails are active for the entire automation session.

        Failures are logged but do not block conversation creation --
        the conversation continues with the user's default security
        analyzer.
        """
        try:
            # Get conversation info to find the sandbox
            async with get_app_conversation_info_service(
                state, request
            ) as info_service:
                conv_info = await info_service.get_app_conversation_info(
                    UUID(conversation_id)
                )
                if not conv_info or not conv_info.sandbox_id:
                    logger.warning(
                        '[Security] Cannot set automation security analyzer: '
                        'conversation %s has no sandbox info',
                        conversation_id,
                        extra=build_log_context(
                            execution_id=execution_id or '',
                            conversation_id=conversation_id,
                            jira_issue_key=jira_issue_key,
                        ),
                    )
                    return

            # Get sandbox details for agent server URL and session key
            async with get_sandbox_service(state, request) as sandbox_service:
                sandbox = await sandbox_service.get_sandbox(conv_info.sandbox_id)
                if not sandbox or not sandbox.exposed_urls:
                    logger.warning(
                        '[Security] Cannot set automation security analyzer: '
                        'sandbox %s has no exposed URLs',
                        conv_info.sandbox_id,
                    )
                    return

                # Extract agent server URL from sandbox exposed URLs
                agent_server_url: str | None = None
                for exposed_url in sandbox.exposed_urls:
                    if exposed_url.name == AGENT_SERVER:
                        agent_server_url = exposed_url.url
                        break

                if not agent_server_url:
                    logger.warning(
                        '[Security] Cannot set automation security analyzer: '
                        'no AGENT_SERVER URL in sandbox %s',
                        conv_info.sandbox_id,
                    )
                    return

                agent_server_url = replace_localhost_hostname_for_docker(
                    agent_server_url
                )

                session_api_key = sandbox.session_api_key
                if not session_api_key:
                    logger.warning(
                        '[Security] Cannot set automation security analyzer: '
                        'sandbox %s has no session API key',
                        conv_info.sandbox_id,
                    )
                    return

            # Build the composite security analyzer
            from openhands.app_server.automation.automation_security_analyzer import (
                AutomationSecurityAnalyzer,
            )
            from openhands.sdk.security import EnsembleSecurityAnalyzer
            from openhands.sdk.security.defense_in_depth import (
                PatternSecurityAnalyzer,
                PolicyRailSecurityAnalyzer,
            )

            security_analyzer = EnsembleSecurityAnalyzer(
                analyzers=[
                    PolicyRailSecurityAnalyzer(),
                    PatternSecurityAnalyzer(),
                    AutomationSecurityAnalyzer(),
                ]
            )

            # Register on the agent server
            async with get_httpx_client(state, request) as httpx_client:
                payload = {'security_analyzer': security_analyzer.model_dump()}
                response = await httpx_client.post(
                    f'{agent_server_url}/api/conversations/'
                    f'{conversation_id}/security_analyzer',
                    json=payload,
                    headers={
                        'X-Session-API-Key': session_api_key,
                    },
                    timeout=30.0,
                )
                response.raise_for_status()

            logger.info(
                '[Security] Automation security analyzer set for conversation %s',
                conversation_id,
                extra=build_log_context(
                    execution_id=execution_id or '',
                    conversation_id=conversation_id,
                    jira_issue_key=jira_issue_key,
                ),
            )

        except Exception as e:
            # Log but don't fail conversation creation
            logger.warning(
                '[Security] Failed to set automation security analyzer for '
                'conversation %s: %s',
                conversation_id,
                e,
                extra=build_log_context(
                    execution_id=execution_id or '',
                    conversation_id=conversation_id,
                    jira_issue_key=jira_issue_key,
                ),
            )
