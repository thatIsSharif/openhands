"""Jira project → repository resolver.

Repository resolution now comes directly from the Jira issue payload,
not from project mappings. This module retains only the reverse lookup
(GitHub repository → Jira project mapping) used by the GitHub webhook
flow to look up per-repository webhook secrets.
"""

from __future__ import annotations

from .execution_store import ExecutionStore


class RepositoryNotResolvedError(Exception):
    """Raised when no repository mapping can be found for a Jira project."""

    def __init__(self, jira_project_key: str, reason: str = ''):
        msg = (
            f'No repository mapping for Jira project "{jira_project_key}". '
            'Create a mapping via POST /api/v1/admin/jira-project-repos.'
        )
        if reason:
            msg += f' {reason}'
        super().__init__(msg)
        self.jira_project_key = jira_project_key


class JiraProjectRepositoryResolver:
    """Resolves the target GitHub repository for a Jira issue.

    Repository resolution comes from the Jira issue payload directly.
    This class only provides the reverse lookup (owner/repo → mapping)
    used by GitHub webhook flows to retrieve per-repository secrets.
    """

    def __init__(self, store: ExecutionStore):
        self.store = store

    async def get_by_repository(
        self,
        owner: str,
        repository: str,
    ):
        return await self.store.get_repository_mapping(
            owner=owner,
            repository=repository,
        )
