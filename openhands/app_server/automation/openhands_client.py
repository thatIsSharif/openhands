from __future__ import annotations

from dataclasses import dataclass

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
from openhands.app_server.automation.callback_processors import (
    AutomationEventCallbackProcessor,
)
from openhands.app_server.config import (
    get_app_conversation_service,
)
from openhands.app_server.integrations.service_types import ProviderType
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
        max_iterations: int | None = None,  # NEW: Max agent turns
        max_budget_per_task: float | None = None,  # NEW: Max $ per task
    ) -> str | None:

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

            processors=[
                AutomationEventCallbackProcessor(),
            ],
            max_iterations=max_iterations,
            max_budget_per_task=max_budget_per_task,
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

                async for task in service.start_app_conversation(
                    start_request
                ):

                    logger.info(
                        f'[Automation] Start task status: {task.status}'
                    )

                    if (
                        task.status
                        == AppConversationStartTaskStatus.READY
                    ):
                        conversation_id = str(
                            task.app_conversation_id
                        )
                        break

                    if (
                        task.status
                        == AppConversationStartTaskStatus.ERROR
                    ):
                        logger.error(
                            '[Automation] Conversation startup failed: '
                            f'{task.detail}'
                        )
                        return None

                if not conversation_id:
                    logger.error(
                        '[Automation] Conversation startup '
                        'finished without READY status'
                    )
                    return None

                logger.info(
                    f'[Automation] Created conversation '
                    f'{conversation_id}',
                    extra=build_log_context(
                        execution_id=execution_id or '',
                        conversation_id=conversation_id,
                        jira_issue_key=jira_issue_key,
                        pr_number=pr_number,
                        repository=repository,
                    ),
                )

                return conversation_id

            except Exception:
                import traceback

                logger.error(traceback.format_exc())
                return None
