"""Tests for GitHubApiService."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openhands.app_server.automation.services.github_api_service import (
    GitHubApiService,
)


@pytest.fixture
def github_service():
    with patch.dict('os.environ', {'GITHUB_TOKEN': 'test-token'}):
        return GitHubApiService()


@pytest.mark.asyncio
async def test_create_pull_request(github_service):
    """create_pull_request calls the GitHub API and returns the response."""
    expected_response = {
        'html_url': 'https://github.com/owner/repo/pull/1',
        'number': 1,
        'title': 'Test PR',
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = expected_response

    with patch('httpx.AsyncClient') as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_resp
        )
        result = await github_service.create_pull_request(
            repo='owner/repo',
            title='Test PR',
            body='PR body',
            head='feature/test',
            base='main',
        )

    assert result == expected_response
    assert result['html_url'] == 'https://github.com/owner/repo/pull/1'


@pytest.mark.asyncio
async def test_create_pull_request_http_error(github_service):
    """HTTP errors are propagated."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        '422 Unprocessable Entity', request=MagicMock(), response=MagicMock()
    )

    with patch('httpx.AsyncClient') as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_resp
        )
        with pytest.raises(httpx.HTTPStatusError):
            await github_service.create_pull_request(
                repo='owner/repo',
                title='Test',
                body='Body',
                head='feature/test',
                base='main',
            )


@pytest.mark.asyncio
async def test_get_pull_request(github_service):
    """get_pull_request fetches PR details."""
    expected = {'number': 42, 'title': 'Test PR'}

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = expected

    with patch('httpx.AsyncClient') as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_resp
        )
        result = await github_service.get_pull_request('owner/repo', 42)

    assert result == expected


@pytest.mark.asyncio
async def test_add_pr_comment(github_service):
    """add_pr_comment posts a comment on the PR."""
    expected = {'id': 123, 'body': 'Nice work!'}

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = expected

    with patch('httpx.AsyncClient') as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=mock_resp
        )
        result = await github_service.add_pr_comment(
            'owner/repo', 42, 'Nice work!'
        )

    assert result == expected


@pytest.mark.asyncio
async def test_update_pr_comment(github_service):
    """update_pr_comment updates an existing comment."""
    expected = {'id': 123, 'body': 'Updated comment'}

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = expected

    with patch('httpx.AsyncClient') as mock_client:
        mock_client.return_value.__aenter__.return_value.patch = AsyncMock(
            return_value=mock_resp
        )
        result = await github_service.update_pr_comment(
            'owner/repo', 123, 'Updated comment'
        )

    assert result == expected


def test_constructor_no_token():
    """GitHubApiService raises ValueError when no token is available."""
    with patch.dict('os.environ', {}, clear=True):
        with pytest.raises(ValueError, match='GitHub token is required'):
            GitHubApiService()
