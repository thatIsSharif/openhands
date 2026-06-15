"""Tests for JiraProjectRepositoryResolver."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from openhands.app_server.automation.execution_models import (
    JiraProjectRepositoryRecord,
)
from openhands.app_server.automation.repository_resolver import (
    JiraProjectRepositoryResolver,
    RepositoryNotResolvedError,
)


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.get_jira_project_repository = AsyncMock()
    return store


@pytest.fixture
def resolver(mock_store):
    return JiraProjectRepositoryResolver(store=mock_store)


class TestResolve:
    async def test_resolves_from_project_mapping(self, resolver, mock_store):
        """When no custom_field_id is configured, uses project mapping."""
        mock_store.get_jira_project_repository.return_value = (
            JiraProjectRepositoryRecord(
                jira_project_key='KAN',
                repository='thatIsSharif/openhands',
                owner='thatIsSharif',
                default_branch='main',
                custom_field_id=None,
            )
        )

        result = await resolver.resolve(
            jira_project_key='KAN',
            issue_payload=None,
        )

        assert result.repository == 'thatIsSharif/openhands'
        assert result.owner == 'thatIsSharif'
        assert result.default_branch == 'main'
        assert result.jira_project_key == 'KAN'
        assert result.resolved_by == 'project_mapping'

    async def test_custom_field_override(self, resolver, mock_store):
        """Custom field value overrides the project mapping."""
        mock_store.get_jira_project_repository.return_value = (
            JiraProjectRepositoryRecord(
                jira_project_key='KAN',
                repository='thatIsSharif/openhands',
                owner='thatIsSharif',
                default_branch='main',
                custom_field_id='customfield_12345',
            )
        )

        payload = {
            'issue': {
                'fields': {
                    'customfield_12345': 'other-owner/other-repo',
                },
            },
        }

        result = await resolver.resolve(
            jira_project_key='KAN',
            issue_payload=payload,
        )

        assert result.repository == 'other-owner/other-repo'
        assert result.owner == 'other-owner'
        assert result.resolved_by == 'custom_field'

    async def test_custom_field_string_value(self, resolver, mock_store):
        """Custom field with a simple string value."""
        mock_store.get_jira_project_repository.return_value = (
            JiraProjectRepositoryRecord(
                jira_project_key='KAN',
                repository='thatIsSharif/openhands',
                owner='thatIsSharif',
                default_branch='main',
                custom_field_id='customfield_12345',
            )
        )

        payload = {
            'issue': {
                'fields': {
                    'customfield_12345': 'custom-owner/custom-repo',
                },
            },
        }

        result = await resolver.resolve(
            jira_project_key='KAN',
            issue_payload=payload,
        )

        assert result.repository == 'custom-owner/custom-repo'

    async def test_custom_field_not_present(self, resolver, mock_store):
        """When custom field is not in payload, falls back to mapping."""
        mock_store.get_jira_project_repository.return_value = (
            JiraProjectRepositoryRecord(
                jira_project_key='KAN',
                repository='thatIsSharif/openhands',
                owner='thatIsSharif',
                default_branch='develop',
                custom_field_id='customfield_99999',
            )
        )

        payload = {
            'issue': {
                'fields': {
                    'summary': 'No custom field here',
                },
            },
        }

        result = await resolver.resolve(
            jira_project_key='KAN',
            issue_payload=payload,
        )

        assert result.repository == 'thatIsSharif/openhands'
        assert result.default_branch == 'develop'
        assert result.resolved_by == 'project_mapping'

    async def test_no_mapping_raises_error(self, resolver, mock_store):
        """When no project mapping exists, raises RepositoryNotResolvedError."""
        mock_store.get_jira_project_repository.return_value = None

        with pytest.raises(
            RepositoryNotResolvedError,
            match='No repository mapping for Jira project "UNKNOWN"',
        ):
            await resolver.resolve(
                jira_project_key='UNKNOWN',
                issue_payload=None,
            )

    async def test_custom_field_invalid_format_falls_back(
        self, resolver, mock_store
    ):
        """Custom field without '/' falls back to project mapping."""
        mock_store.get_jira_project_repository.return_value = (
            JiraProjectRepositoryRecord(
                jira_project_key='KAN',
                repository='thatIsSharif/openhands',
                owner='thatIsSharif',
                default_branch='main',
                custom_field_id='customfield_12345',
            )
        )

        payload = {
            'issue': {
                'fields': {
                    'customfield_12345': 'not-valid-format',
                },
            },
        }

        result = await resolver.resolve(
            jira_project_key='KAN',
            issue_payload=payload,
        )

        # Falls back to project mapping because custom field is not owner/repo
        assert result.repository == 'thatIsSharif/openhands'
        assert result.resolved_by == 'project_mapping'


class TestExtractCustomField:
    def test_string_value(self, resolver):
        payload = {
            'issue': {
                'fields': {
                    'customfield_42': 'owner/repo',
                },
            },
        }
        assert (
            resolver._extract_custom_field(payload, 'customfield_42')
            == 'owner/repo'
        )

    def test_dict_value(self, resolver):
        payload = {
            'issue': {
                'fields': {
                    'customfield_42': {'value': 'owner/repo'},
                },
            },
        }
        assert (
            resolver._extract_custom_field(payload, 'customfield_42')
            == 'owner/repo'
        )

    def test_dict_with_name(self, resolver):
        payload = {
            'issue': {
                'fields': {
                    'customfield_42': {'name': 'owner/repo'},
                },
            },
        }
        assert (
            resolver._extract_custom_field(payload, 'customfield_42')
            == 'owner/repo'
        )

    def test_missing_field(self, resolver):
        assert (
            resolver._extract_custom_field({}, 'customfield_42') is None
        )

    def test_null_value(self, resolver):
        payload = {
            'issue': {
                'fields': {
                    'customfield_42': None,
                },
            },
        }
        assert (
            resolver._extract_custom_field(payload, 'customfield_42') is None
        )
