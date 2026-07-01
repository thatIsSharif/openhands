from fastapi import APIRouter

from openhands.app_server.app_conversation import app_conversation_router
# Import AutomationEventCallbackProcessor at startup so the discriminated
# union registry knows about it before any EventCallbacks (stored in the
# database from prior deployments) are deserialized.
from openhands.app_server.automation.callback_processors import (
    AutomationEventCallbackProcessor,  # noqa: F401
)
from openhands.app_server.automation.admin_router import (
    router as admin_router,
)
from openhands.app_server.automation.github_webhook_router import (
    router as github_webhook_router,
)
from openhands.app_server.automation.jira_webhook_router import (
    router as jira_webhook_router,
)
from openhands.app_server.config_api.config_router import router as config_router
from openhands.app_server.event import event_router
from openhands.app_server.event_callback import (
    webhook_router,
)
from openhands.app_server.git.git_router import router as git_router
from openhands.app_server.pending_messages.pending_message_router import (
    router as pending_message_router,
)
from openhands.app_server.sandbox import sandbox_router, sandbox_spec_router
from openhands.app_server.secrets.secrets_router import (
    router as secrets_router,
)
from openhands.app_server.settings.settings_router import (
    router as settings_router,
)
from openhands.app_server.user import skills_router, user_router
from openhands.app_server.web_client import web_client_router

# Include routers
router = APIRouter(prefix='/api/v1')
router.include_router(event_router.router)
router.include_router(app_conversation_router.router)
router.include_router(pending_message_router)
router.include_router(sandbox_router.router)
router.include_router(sandbox_spec_router.router)
router.include_router(settings_router)
router.include_router(secrets_router)
router.include_router(user_router.router)
router.include_router(skills_router.router)
router.include_router(webhook_router.router)
router.include_router(web_client_router.router)
router.include_router(git_router)
router.include_router(config_router)
router.include_router(jira_webhook_router)
router.include_router(github_webhook_router)
router.include_router(admin_router)
