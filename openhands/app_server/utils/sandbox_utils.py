"""Sandbox utility functions for the automation platform.

Provides helpers for pausing sandboxes after task completion
across Jira and GitHub automation flows.
"""

from openhands.app_server.utils.logger import openhands_logger as logger


async def pause_sandbox(sandbox_id: str, state, request) -> None:
    """Pause a sandbox if it is currently running.

    Args:
        sandbox_id: The sandbox ID to pause.
        state: InjectorState or request.state for dependency injection.
        request: The FastAPI request object.
    """
    if not sandbox_id:
        logger.warning('[Automation] No sandbox_id provided, skipping pause')
        return

    from openhands.app_server.config import get_sandbox_service
    from openhands.app_server.sandbox.sandbox_models import SandboxStatus

    async with get_sandbox_service(state, request) as sandbox_service:
        sandbox = await sandbox_service.get_sandbox(sandbox_id)
        if sandbox is None:
            logger.warning(
                '[Automation] Sandbox %s not found, cannot pause',
                sandbox_id,
            )
            return

        if sandbox.status == SandboxStatus.RUNNING:
            logger.info('[Automation] Pausing sandbox %s', sandbox_id)
            await sandbox_service.pause_sandbox(sandbox_id)
        else:
            logger.info(
                '[Automation] Sandbox %s status is %s, not pausing',
                sandbox_id,
                sandbox.status.value,
            )
