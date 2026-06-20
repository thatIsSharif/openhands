"""Jira project → repository resolver.

Repository resolution now comes directly from the Jira issue payload,
not from project mappings. This module retains only the reverse lookup
(GitHub repository → Jira project mapping) used by the GitHub webhook
flow to look up per-repository webhook secrets.

The ``JiraProjectRepositoryResolver`` class has been deprecated in favor
of calling ``ExecutionStore.get_repository_mapping()`` directly.
"""

from __future__ import annotations

from .execution_store import ExecutionStore


class RepositoryNotResolvedError(Exception):
    """Raised when no repository mapping can be found for a Jira project.

    .. deprecated::
        This exception is no longer raised by any production code path.
        Repository resolution is now handled directly via the Jira issue
        payload, not via project-level mappings.
    """

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

    .. deprecated::
        Use ``ExecutionStore.get_repository_mapping()`` directly instead.
        This class is retained only for backward compatibility.

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
        """Look up a repository mapping by owner and repository name.

        .. deprecated::
            Call ``store.get_repository_mapping(owner, repository)`` directly.
        """
        return await self.store.get_repository_mapping(
            owner=owner,
            repository=repository,
        )
