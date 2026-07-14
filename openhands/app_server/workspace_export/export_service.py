"""Service that orchestrates a full workspace export.

Steps
-----
1. Resolve the conversation info and sandbox id.
2. Docker commit the sandbox container to an image.
3. Docker save the image to a tar byte stream.
4. Serialise the conversation JSON.
5. Fetch and serialise conversation events.
6. Store everything via the configured ``StorageBackend``.
7. Delete the sandbox container to free resources.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

import docker

from openhands.app_server.app_conversation.app_conversation_info_service import (
    AppConversationInfoService,
)
from openhands.app_server.app_conversation.app_conversation_service import (
    AppConversationService,
)
from openhands.app_server.config import get_global_config
from openhands.app_server.conversation_paths import V1_CONVERSATIONS_DIR
from openhands.app_server.sandbox.sandbox_service import SandboxService
from openhands.app_server.workspace_export.storage_backend import (
    SnapshotMetadata,
    StorageBackend,
)
from openhands.sdk import Event

_logger = logging.getLogger(__name__)


@dataclass
class ExportResult:
    success: bool
    snapshot_tag: Optional[str] = None
    error_message: Optional[str] = None
    exported_at: Optional[str] = field(default=None)


class WorkspaceExportService:
    """Orchestrate a workspace export."""

    def __init__(
        self,
        storage_backend: StorageBackend,
        docker_client: Optional[docker.DockerClient] = None,
        max_image_size_mb: int = 5000,
        snapshot_prefix: str = 'oh-export-',
    ) -> None:
        self._storage = storage_backend
        self._docker = docker_client or self._get_docker_client()
        self._max_image_size_mb = max_image_size_mb
        self._snapshot_prefix = snapshot_prefix

    @staticmethod
    def _get_docker_client() -> docker.DockerClient:
        return docker.from_env(timeout=300)

    async def _load_events_from_disk(
        self,
        conversation_id: UUID,
        created_by_user_id: str | None,
    ) -> list[dict] | None:
        """Read conversation event files directly from disk.

        Events are stored by the webhook handler as individual JSON files
        at ``{persistence_dir}/{created_by_user_id}/v1_conversations/
        {conversation_id_hex}/``.  This avoids the injection-layer user-
        context mismatch that can occur when using ``EventService``.

        Returns ``None`` when the directory is absent (e.g. S3/GCP-backed
        deployments) so callers can fall back gracefully.
        """
        if not created_by_user_id:
            _logger.warning(
                'No created_by_user_id for %s — cannot load events',
                conversation_id,
            )
            return None

        prefix = get_global_config().persistence_dir
        conv_path = prefix / created_by_user_id / V1_CONVERSATIONS_DIR / conversation_id.hex

        if not conv_path.is_dir():
            _logger.warning(
                'Event directory not found: %s', conv_path
            )
            return None

        raw_events: list[dict] = []
        for path in sorted(conv_path.iterdir()):
            if path.suffix == '.json':
                try:
                    content = path.read_text()
                    event = Event.model_validate_json(content)
                    raw_events.append(event.model_dump(mode='json'))
                except Exception:
                    _logger.exception('Error reading event %s', path)

        _logger.info(
            'Loaded %d events from %s', len(raw_events), conv_path
        )
        return raw_events

    async def export_conversation(
        self,
        conversation_id: UUID,
        jira_key: str,
        app_conversation_service: AppConversationService,
        app_conversation_info_service: AppConversationInfoService,
        docker_sandbox_service: SandboxService,
    ) -> ExportResult:
        if not jira_key or not jira_key.strip():
            return ExportResult(
                success=False,
                error_message='Jira issue key is empty — skipping export',
            )

        # 1. Resolve conversation info
        info = await app_conversation_info_service.get_app_conversation_info(
            conversation_id
        )
        if not info:
            return ExportResult(
                success=False,
                error_message=f'Conversation {conversation_id} not found',
            )

        sandbox_id = info.sandbox_id
        if not sandbox_id:
            return ExportResult(
                success=False,
                error_message='Conversation has no sandbox — nothing to export',
            )

        # 2. Docker commit the sandbox container
        try:
            snapshot_tag = await docker_sandbox_service.snapshot_sandbox(sandbox_id)
            if not snapshot_tag:
                return ExportResult(
                    success=False,
                    error_message=f'Failed to snapshot sandbox {sandbox_id}',
                )
        except Exception as exc:
            _logger.exception('Error snapshotting sandbox %s', sandbox_id)
            return ExportResult(
                success=False,
                error_message=f'Snapshot error: {exc}',
            )

        # 3. Docker save the committed image to a tar stream
        try:
            image = self._docker.images.get(snapshot_tag)
            tar_stream: io.BytesIO = io.BytesIO()
            for chunk in image.save(chunk_size=self._max_image_size_mb * 1024 * 1024):
                tar_stream.write(chunk)
            image_tar = tar_stream.getvalue()
        except Exception as exc:
            _logger.exception('Error saving image %s', snapshot_tag)
            return ExportResult(
                success=False,
                error_message=f'Image save error: {exc}',
            )

        # 4. Serialise conversation state
        try:
            conversation_data = {
                'conversation_id': str(conversation_id),
                'jira_issue_key': jira_key,
                'sandbox_id': sandbox_id,
                'model': info.llm_model,
                'title': info.title,
            }
            conversation_json = json.dumps(conversation_data, indent=2)
        except Exception as exc:
            _logger.exception('Error serialising conversation')
            return ExportResult(
                success=False,
                error_message=f'Serialisation error: {exc}',
            )

        # 5. Fetch and serialise conversation events so they can be
        #    restored alongside the sandbox filesystem snapshot.
        #    Read event files directly from the filesystem using the
        #    conversation owner's user_id to match the path used by
        #    the webhook handler (which stores events under
        #    sandbox_record.created_by_user_id).
        events_json: str | None = None
        try:
            raw_events = await self._load_events_from_disk(
                conversation_id=conversation_id,
                created_by_user_id=info.created_by_user_id,
            )
            if raw_events is not None:
                events_json = json.dumps(raw_events, indent=2)
                _logger.info(
                    'Serialised %d events for %s',
                    len(raw_events),
                    conversation_id,
                )
        except Exception:
            _logger.exception('Error fetching events for %s', conversation_id)

        # 6. Store everything
        import time

        # Store the git provider string for backward-compat serialisation
        git_provider_str = (
            info.git_provider.value
            if info.git_provider
            else None
        )
        metadata = SnapshotMetadata(
            jira_key=jira_key,
            snapshot_tag=snapshot_tag,
            created_at=str(int(time.time())),
            conversation_id=str(conversation_id),
            llm_model=info.llm_model,
            sandbox_id=sandbox_id,
            selected_repository=info.selected_repository,
            selected_branch=info.selected_branch,
            git_provider=git_provider_str,
        )

        ok = await self._storage.save_snapshot(
            jira_key=jira_key,
            image_tar=image_tar,
            conversation_json=conversation_json,
            metadata=metadata,
        )
        if not ok:
            return ExportResult(
                success=False,
                error_message='Storage backend failed to save snapshot',
            )

        # Save events alongside the snapshot if they were serialised.
        if events_json:
            events_ok = await self._storage.save_events_json(
                jira_key, events_json
            )
            if not events_ok:
                _logger.warning('Failed to save events for %s', jira_key)

        # 7. Delete the sandbox to free resources
        try:
            await docker_sandbox_service.delete_sandbox(sandbox_id)
        except Exception as exc:
            _logger.warning(
                'Failed to delete sandbox %s after export: %s', sandbox_id, exc
            )

        return ExportResult(
            success=True,
            snapshot_tag=snapshot_tag,
            exported_at=metadata.created_at,
        )
