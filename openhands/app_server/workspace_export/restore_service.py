"""Service that restores a workspace from a previously-exported snapshot.

Steps
-----
1. Load the snapshot tar and conversation JSON from storage.
2. Docker load the image from the tar.
3. Use ``SandboxService.restore_from_snapshot`` to create a new sandbox.
4. Return sandbox info so the caller can wire it into a new conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import docker

from openhands.app_server.sandbox.sandbox_service import SandboxInfo, SandboxService
from openhands.app_server.workspace_export.storage_backend import StorageBackend

_logger = logging.getLogger(__name__)


@dataclass
class RestoreResult:
    success: bool
    sandbox_info: Optional[SandboxInfo] = None
    error_message: Optional[str] = None


class WorkspaceRestoreService:
    """Restore a sandbox from a stored snapshot."""

    def __init__(
        self,
        storage_backend: StorageBackend,
        docker_client: Optional[docker.DockerClient] = None,
    ) -> None:
        self._storage = storage_backend
        self._docker = docker_client or docker.from_env()

    async def restore_conversation(
        self,
        jira_key: str,
        new_sandbox_id: str,
        docker_sandbox_service: SandboxService,
    ) -> RestoreResult:
        if not await self._storage.exists(jira_key):
            return RestoreResult(
                success=False,
                error_message=f'No snapshot exists for {jira_key}',
            )

        # 1. Load the tar
        image_tar = await self._storage.load_image_tar(jira_key)
        if image_tar is None:
            return RestoreResult(
                success=False,
                error_message=f'Failed to load image tar for {jira_key}',
            )

        # 2. Docker load
        try:
            loaded = self._docker.images.load(image_tar)
            if not loaded:
                return RestoreResult(
                    success=False,
                    error_message='Docker load returned no images',
                )
            loaded_tag = loaded[0].tags[0] if loaded[0].tags else None
            _logger.info(
                'Loaded image for %s: %s', jira_key, loaded_tag or '(untagged)'
            )
        except Exception as exc:
            _logger.exception('Error loading image for %s', jira_key)
            return RestoreResult(
                success=False,
                error_message=f'Docker load error: {exc}',
            )

        # 3. Restore the sandbox from the loaded image
        try:
            sandbox_info = await docker_sandbox_service.restore_from_snapshot(
                new_sandbox_id
            )
            if not sandbox_info:
                return RestoreResult(
                    success=False,
                    error_message='Sandbox restore returned None',
                )
        except Exception as exc:
            _logger.exception('Error restoring sandbox for %s', jira_key)
            return RestoreResult(
                success=False,
                error_message=f'Sandbox restore error: {exc}',
            )

        return RestoreResult(success=True, sandbox_info=sandbox_info)
