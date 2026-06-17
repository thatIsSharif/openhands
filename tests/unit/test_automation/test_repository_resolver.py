"""Tests for JiraProjectRepositoryResolver.

The resolver no longer performs cascading resolution (custom field
override → project mapping). Tests focus on the reverse lookup
used by the GitHub webhook flow to retrieve per-repository secrets.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from openhands.app_server.automation.repository_resolver import (
    JiraProjectRepositoryResolver,
)


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.get_repository_mapping = AsyncMock()
    return store


@pytest.fixture
def resolver(mock_store):
    return JiraProjectRepositoryResolver(store=mock_store)


class TestGetByRepository:
    async def test_finds_mapping(self, resolver, mock_store):
        """Returns the mapping when found."""
        from unittest.mock import MagicMock

        mock_mapping = MagicMock()
        mock_mapping.owner = 'thatIsSharif'
        mock_mapping.repository = 'dsd-frontend'
        mock_store.get_repository_mapping.return_value = mock_mapping

        result = await resolver.get_by_repository(
            owner='thatIsSharif',
            repository='dsd-frontend',
        )

        assert result is not None
        assert result.owner == 'thatIsSharif'
        assert result.repository == 'dsd-frontend'
        mock_store.get_repository_mapping.assert_called_once_with(
            owner='thatIsSharif',
            repository='dsd-frontend',
        )

    async def test_returns_none_when_not_found(self, resolver, mock_store):
        """Returns None when no mapping exists."""
        mock_store.get_repository_mapping.return_value = None

        result = await resolver.get_by_repository(
            owner='unknown',
            repository='unknown',
        )

        assert result is None
