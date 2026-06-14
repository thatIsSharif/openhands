"""OpenHands client - helper for creating automation conversations.

Wraps the OSS AppConversationService to create conversations with the
AUTOMATION trigger type and propagate correlation metadata.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from openhands.agent_server.models import SendMessageRequest, TextContent
from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversationStartRequest,
    AppConversationTrigger,
)
from openhands.app_server.automation.callback_processors import (
    AutomationEventCallbackProcessor,
)
from openhands.app_server.integrations.provider import ProviderType
from openhands.app_server.utils.logger import openhands_logger as logger

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
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        git_provider: ProviderType | None = None,
    ) -> str | None:
        """Create a new OpenHands conversation for an automation task.

        Uses the OSS AppConversationService to start a conversation with
        AUTOMATION trigger type. Registers an AutomationEventCallbackProcessor
        to update the execution record when the conversation reaches a terminal
        state.

        Args:
            state: The injector state.
            request: The FastAPI request object.
            prompt: The initial prompt for the agent.
            title: The conversation title.
            execution_id: The execution correlation ID.
            jira_issue_key: The Jira issue key, if applicable.
            pr_number: The PR number, if applicable.
            repository: The repository name (for logging).
            selected_repository: The repository to clone (owner/repo).
            selected_branch: The branch to check out.
            git_provider: The Git provider (e.g., ProviderType.GITHUB).

        Returns:
            Conversation ID string, or None if creation failed.
        """
        from openhands.app_server.config import get_app_conversation_service

        start_request = AppConversationStartRequest(
            trigger=AppConversationTrigger.AUTOMATION,
            title=title,
            initial_message=SendMessageRequest(
                content=[
                    TextContent(
                        text=prompt,
                    )
                ],
            ),
            selected_repository=selected_repository,
            selected_branch=selected_branch,
            git_provider=git_provider,
            processors=[
                AutomationEventCallbackProcessor(),
            ],
        )

        async with get_app_conversation_service(state, request) as service:
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
                conversation = await service.start_app_conversation(
                    start_request
                )
                conversation_id = conversation.conversation_id
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
                return conversation_id
            except Exception as e:
                logger.error(
                    f'[Automation] Failed to create conversation: {e}',
                    extra=build_log_context(
                        execution_id=execution_id or '',
                        jira_issue_key=jira_issue_key,
                    ),
                )
                return None
