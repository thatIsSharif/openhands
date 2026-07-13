"""Workspace export/restore system for container state snapshots.

Uses Docker commit/save/load to snapshot and restore the entire container
filesystem, organized by Jira issue key.

Submodules use heavy dependencies (FastAPI, Docker SDK, agent_server).
Import them lazily when needed — the ``__init__.py`` does not import them
eagerly so that ``StorageBackend`` / ``LocalStorage`` can be imported
without pulling in the full server dependency tree.
"""

from .storage_backend import StorageBackend, SnapshotMetadata  # noqa: F401

__all__ = [
    'ExportOnCompletionCallbackProcessor',
    'LocalStorage',
    'S3Storage',
    'SnapshotMetadata',
    'StorageBackend',
    'WorkspaceExportService',
    'WorkspaceRestoreService',
    'export_restore_router',
]
