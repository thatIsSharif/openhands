"""S3 storage backend for workspace snapshots.

This is a stub / placeholder for production use.
"""

from __future__ import annotations

import logging
from typing import Optional

from openhands.app_server.workspace_export.storage_backend import (
    SnapshotMetadata,
    StorageBackend,
)

_logger = logging.getLogger(__name__)


class S3Storage(StorageBackend):
    """Stores snapshots in an S3 bucket.

    NOTE: This is a stub.  Production implementation needs:

    * ``boto3`` client configured with bucket name and optional prefix.
    * ``save_snapshot`` → ``put_object`` for each artifact.
    * ``load_*`` → ``get_object`` calls.
    * ``exists`` → ``head_object``.
    """

    def __init__(self, bucket: str, prefix: str = 'exports/') -> None:
        self._bucket = bucket
        self._prefix = prefix
        _logger.warning(
            'S3Storage is a stub — no data will actually be persisted '
            'to bucket %s',
            bucket,
        )

    async def save_snapshot(
        self,
        jira_key: str,
        image_tar: bytes,
        conversation_json: str,
        metadata: SnapshotMetadata,
    ) -> bool:
        _logger.info('[stub] S3 save_snapshot(%s) called — not persisted', jira_key)
        return True

    async def load_image_tar(self, jira_key: str) -> bytes | None:
        _logger.info('[stub] S3 load_image_tar(%s) called', jira_key)
        return None

    async def load_conversation_json(self, jira_key: str) -> str | None:
        _logger.info('[stub] S3 load_conversation_json(%s) called', jira_key)
        return None

    async def load_metadata(self, jira_key: str) -> SnapshotMetadata | None:
        _logger.info('[stub] S3 load_metadata(%s) called', jira_key)
        return None

    async def exists(self, jira_key: str) -> bool:
        _logger.info('[stub] S3 exists(%s) called', jira_key)
        return False

    async def delete(self, jira_key: str) -> bool:
        _logger.info('[stub] S3 delete(%s) called', jira_key)
        return True
