"""Abstract storage backend for workspace snapshots."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class SnapshotMetadata:
    """Metadata stored alongside each snapshot."""

    jira_key: str
    snapshot_tag: str
    created_at: str
    export_version: str = '1.0'
    conversation_id: Optional[str] = None
    llm_model: Optional[str] = None
    sandbox_id: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> 'SnapshotMetadata':
        return cls(**json.loads(raw))


class StorageBackend(ABC):
    """Interface for storing and loading workspace snapshots."""

    @abstractmethod
    async def save_snapshot(
        self,
        jira_key: str,
        image_tar: bytes,
        conversation_json: str,
        metadata: SnapshotMetadata,
    ) -> bool:
        ...

    @abstractmethod
    async def load_image_tar(self, jira_key: str) -> bytes | None:
        ...

    @abstractmethod
    async def load_conversation_json(self, jira_key: str) -> str | None:
        ...

    @abstractmethod
    async def load_metadata(self, jira_key: str) -> SnapshotMetadata | None:
        ...

    @abstractmethod
    async def exists(self, jira_key: str) -> bool:
        ...

    @abstractmethod
    async def delete(self, jira_key: str) -> bool:
        ...
