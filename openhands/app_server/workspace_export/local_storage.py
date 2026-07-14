"""Local filesystem storage backend for workspace snapshots."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from openhands.app_server.workspace_export.storage_backend import (
    SnapshotMetadata,
    StorageBackend,
)

_logger = logging.getLogger(__name__)


def _sanitize_jira_key(key: str) -> str:
    """Replace characters that are problematic in filenames."""
    sanitized = re.sub(r'[^\w.-]+', '_', key)
    return sanitized if sanitized else '_'


class LocalStorage(StorageBackend):
    """Stores snapshots on the local filesystem.

    Directory layout::

        {export_dir}/conversations/{jira_key}/
            metadata.json
            conversation.json
            snapshot.tar
    """

    def __init__(self, export_dir: str) -> None:
        self._root = Path(export_dir)

    def _conversation_dir(self, jira_key: str) -> Path:
        safe = _sanitize_jira_key(jira_key)
        return self._root / 'conversations' / safe

    async def save_snapshot(
        self,
        jira_key: str,
        image_tar: bytes,
        conversation_json: str,
        metadata: SnapshotMetadata,
    ) -> bool:
        conv_dir = self._conversation_dir(jira_key)
        conv_dir.mkdir(parents=True, exist_ok=True)

        try:
            with open(conv_dir / 'metadata.json', 'w') as f:
                f.write(metadata.to_json())
            with open(conv_dir / 'conversation.json', 'w') as f:
                f.write(conversation_json)
            with open(conv_dir / 'snapshot.tar', 'wb') as f:
                f.write(image_tar)
            _logger.info('Saved snapshot for %s at %s', jira_key, conv_dir)
            return True
        except OSError as exc:
            _logger.error('Failed to save snapshot for %s: %s', jira_key, exc)
            return False

    async def exists(self, jira_key: str) -> bool:
        conv_dir = self._conversation_dir(jira_key)
        return (
            conv_dir.is_dir()
            and (conv_dir / 'metadata.json').is_file()
            and (conv_dir / 'snapshot.tar').is_file()
        )

    async def load_image_tar(self, jira_key: str) -> bytes | None:
        path = self._conversation_dir(jira_key) / 'snapshot.tar'
        if not path.is_file():
            return None
        try:
            return path.read_bytes()
        except OSError:
            _logger.exception('Error reading %s', path)
            return None

    async def load_conversation_json(self, jira_key: str) -> str | None:
        path = self._conversation_dir(jira_key) / 'conversation.json'
        if not path.is_file():
            return None
        try:
            return path.read_text()
        except OSError:
            _logger.exception('Error reading %s', path)
            return None

    async def load_metadata(self, jira_key: str) -> SnapshotMetadata | None:
        path = self._conversation_dir(jira_key) / 'metadata.json'
        if not path.is_file():
            return None
        try:
            return SnapshotMetadata.from_json(path.read_text())
        except (OSError, json.JSONDecodeError, KeyError):
            _logger.exception('Error reading metadata for %s', jira_key)
            return None

    async def save_events_json(
        self, jira_key: str, events_json: str
    ) -> bool:
        """Save serialized conversation events alongside the snapshot."""
        conv_dir = self._conversation_dir(jira_key)
        conv_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(conv_dir / 'events.json', 'w') as f:
                f.write(events_json)
            _logger.info('Saved events for %s', jira_key)
            return True
        except OSError as exc:
            _logger.error('Failed to save events for %s: %s', jira_key, exc)
            return False

    async def load_events_json(self, jira_key: str) -> str | None:
        """Load serialized conversation events for a snapshot."""
        path = self._conversation_dir(jira_key) / 'events.json'
        if not path.is_file():
            return None
        try:
            return path.read_text()
        except OSError:
            _logger.exception('Error reading events for %s', jira_key)
            return None

    async def delete(self, jira_key: str) -> bool:
        conv_dir = self._conversation_dir(jira_key)
        if not conv_dir.is_dir():
            return False
        try:
            import shutil
            shutil.rmtree(conv_dir)
            _logger.info('Deleted snapshot for %s', jira_key)
            return True
        except OSError as exc:
            _logger.error('Failed to delete snapshot for %s: %s', jira_key, exc)
            return False
