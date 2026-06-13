"""OpenHands client for creating and managing automation conversations.

Wraps the existing AppConversationService conversation creation flow
for use by the automation platform services.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from jinja2 import Environment, FileSystemLoader

from openhands.app_server.utils.logger import openhands_logger as logger

from .correlation import build_log_context, generate_conversation_title


OPENHANDS_RESOLVER_TEMPLATES_DIR = (
    'openhands/app_server/integrations/templates/resolver/'
)


class OpenHandsClient:
    """Client for creating and managing OpenHands conversations.

    Provides a simplified interface over the existing AppConversationService
    for the automation platform, without depending on the integration view classes.
    """

    def __init__(self) -> None:
        self._jinja_env = Environment(
            loader=FileSystemLoader(OPENHANDS_RESOLVER_TEMPLATES_DIR)
        )

    def get_template_env(self) -> Environment:
        """Return the Jinja2 environment for rendering automation templates."""
        return self._jinja_env

    async def create_conversation(
        self,
        execution_id: str,
        prompt: str,
        repository: str | None = None,
        branch: str | None = None,
        pr_number: int | None = None,
        jira_issue_key: str | None = None,
        conversation_id: UUID | None = None,
    ) -> str | None:
        """Create and start an OpenHands conversation for the automation.

        Args:
            execution_id: The execution correlation ID.
            prompt: The initial user message for the agent.
            repository: Repository name (org/repo) to clone.
            branch: Branch to check out.
            pr_number: Associated PR number.
            jira_issue_key: Associated Jira issue key.
            conversation_id: Optional pre-generated conversation ID.

        Returns:
            The conversation ID (string) if successful, None otherwise.
        """
        from openhands.app_server.app_conversation.app_conversation_models import (
            AppConversationStartRequest,
            ConversationTrigger,
        )
        from openhands.app_server.app_conversation.app_conversation_models import (
            SendMessageRequest,
        )
        from openhands.app_server.app_conversation.app_conversation_models import (
            TextContent,
        )

        title = generate_conversation_title(
            source_type='automation',
            jira_issue_key=jira_issue_key,
            pr_number=pr_number,
        )

        conv_id = conversation_id or uuid4()

        start_request = AppConversationStartRequest(
            conversation_id=conv_id,
            initial_message=SendMessageRequest(
                role='user',
                content=[TextContent(text=prompt)],
            ),
            selected_repository=repository,
            selected_branch=branch,
            pr_number=[pr_number] if pr_number else [],
            trigger=ConversationTrigger.AUTOMATION,
            title=title,
        )

        log_ctx = build_log_context(
            execution_id=execution_id,
            conversation_id=str(conv_id),
            repository=repository,
            branch=branch,
            jira_issue_key=jira_issue_key,
            pr_number=pr_number,
        )

        try:
            from openhands.app_server.app_conversation.app_conversation_service import (
                get_app_conversation_service,
            )
            from openhands.app_server.app_conversation.app_conversation_models import (
                AppConversationStartTaskStatus,
            )
            from openhands.app_server.services.injector import InjectorState

            injector_state = InjectorState()

            async with get_app_conversation_service(
                injector_state
            ) as app_conversation_service:
                async for task in app_conversation_service.start_app_conversation(
                    start_request
                ):
                    if task.status == AppConversationStartTaskStatus.ERROR:
                        logger.error(
                            f'[Automation] Failed to start conversation: '
                            f'{task.detail}',
                            extra=log_ctx,
                        )
                        return None

            logger.info(
                f'[Automation] Created conversation {conv_id} for '
                f'execution {execution_id}',
                extra=log_ctx,
            )
            return str(conv_id)

        except Exception as e:
            logger.error(
                f'[Automation] Error creating conversation: {e}',
                extra=log_ctx,
            )
            return None
