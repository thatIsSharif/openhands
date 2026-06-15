"""Jira project → repository resolver.

Resolves the target GitHub repository for a Jira issue using
a cascading lookup strategy:

1. Issue-level custom field (per-issue override)
2. Project-level mapping table (DB-backed)
3. Fail with RepositoryNotResolvedError (never silently defaults)
"""

from __future__ import annotations

from dataclasses import dataclass

from openhands.app_server.utils.logger import openhands_logger as logger

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


@dataclass
class ResolvedRepository:
    """The result of a successful repository resolution."""

    repository: str  # "owner/repo-name"
    owner: str       # "thatIsSharif"
    default_branch: str  # "main" (or configured default)
    jira_project_key: str
    resolved_by: str  # "custom_field" | "project_mapping"


@dataclass
class JiraProjectRepositoryResolver:
    """Resolves the target GitHub repository for a Jira issue.

    Cascading resolution order:
    1. Check the Jira issue's custom field (if configured for this project)
    2. Check the project→repository mapping table
    3. Raise RepositoryNotResolvedError
    """

    store: ExecutionStore

    async def resolve(
        self,
        jira_project_key: str,
        issue_payload: dict | None = None,
    ) -> ResolvedRepository:
        """Resolve repository info for a Jira project.

        Args:
            jira_project_key: The Jira project key (e.g. "KAN").
            issue_payload: The full Jira webhook payload (needed to
                read custom fields for per-issue overrides).

        Returns:
            ResolvedRepository with repository, owner, default_branch.

        Raises:
            RepositoryNotResolvedError: If no mapping can be found.
        """
        # Step 1: Look up the project mapping from the database
        mapping = await self.store.get_jira_project_repository(jira_project_key)

        if mapping is None:
            raise RepositoryNotResolvedError(jira_project_key)

        # Step 2: Check for a per-issue custom field override
        if mapping.custom_field_id and issue_payload:
            override = self._extract_custom_field(
                issue_payload, mapping.custom_field_id
            )
            if override:
                # Custom field value should be "owner/repo-name" format
                parts = override.strip().split('/', 1)
                if len(parts) == 2:
                    logger.info(
                        f'[Automation] Repository override from custom field '
                        f'{mapping.custom_field_id}: {override} '
                        f'(project: {jira_project_key})'
                    )
                    return ResolvedRepository(
                        repository=override,
                        owner=parts[0],
                        default_branch=mapping.default_branch,
                        jira_project_key=jira_project_key,
                        resolved_by='custom_field',
                    )
                else:
                    logger.warning(
                        f'[Automation] Custom field {mapping.custom_field_id} '
                        f'value "{override}" is not in "owner/repo" format. '
                        f'Falling back to project mapping.'
                    )

        # Step 3: Return the project-level mapping
        return ResolvedRepository(
            repository=mapping.repository,
            owner=mapping.owner,
            default_branch=mapping.default_branch,
            jira_project_key=jira_project_key,
            resolved_by='project_mapping',
        )

    @staticmethod
    def _extract_custom_field(
        payload: dict,
        custom_field_id: str,
    ) -> str | None:
        """Extract a custom field value from a Jira issue payload.

        Jira custom fields appear as top-level keys in the fields object
        with the format ``customfield_12345``.
        """
        fields = payload.get('issue', {}).get('fields', {})
        value = fields.get(custom_field_id)
        if value is None:
            return None
        # Custom fields can be strings or objects with a "value" key
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get('value') or value.get('name')
        return str(value) if value else None

    async def get_by_repository(
        self,
        owner: str,
        repository: str,
    ):
        return await self.store.get_repository_mapping(
            owner=owner,
            repository=repository,
        )
