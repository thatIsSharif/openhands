"""GitHub API service for the automation platform.

Handles all deterministic GitHub operations (PR creation, comments)
that were previously handled by the LLM.
"""

from __future__ import annotations

import os

import httpx


class GitHubApiService:
    """Concrete service for GitHub API operations.

    Uses direct httpx calls to the GitHub REST API authenticated
    via the GITHUB_TOKEN environment variable.
    """

    BASE_URL = 'https://api.github.com'

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get('GITHUB_TOKEN', '')
        if not self._token:
            raise ValueError(
                'GitHub token is required. Set GITHUB_TOKEN environment variable '
                'or pass token to constructor.'
            )

    def _headers(self) -> dict[str, str]:
        return {
            'Authorization': f'Bearer {self._token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }

    async def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict:
        """Create a pull request on GitHub.

        Args:
            repo: Full repository name (e.g. 'owner/repo').
            title: PR title.
            body: PR description body.
            head: The name of the branch where changes are implemented.
            base: The name of the branch you want the changes pulled into.

        Returns:
            The GitHub API response dict, including 'html_url' for the PR URL.

        Raises:
            httpx.HTTPStatusError: If the API call fails.
        """
        url = f'{self.BASE_URL}/repos/{repo}/pulls'
        payload = {
            'title': title,
            'body': body,
            'head': head,
            'base': base,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, json=payload, headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()

    async def get_pull_request(self, repo: str, pr_number: int) -> dict:
        """Get details of a pull request.

        Args:
            repo: Full repository name (e.g. 'owner/repo').
            pr_number: Pull request number.

        Returns:
            The GitHub API response dict.
        """
        url = f'{self.BASE_URL}/repos/{repo}/pulls/{pr_number}'
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def add_pr_comment(
        self, repo: str, pr_number: int, body: str
    ) -> dict:
        """Add a comment to a pull request.

        Args:
            repo: Full repository name (e.g. 'owner/repo').
            pr_number: Pull request number.
            body: Comment text.

        Returns:
            The GitHub API response dict.
        """
        url = f'{self.BASE_URL}/repos/{repo}/issues/{pr_number}/comments'
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, json={'body': body}, headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()

    async def update_pr_comment(
        self, repo: str, comment_id: int, body: str
    ) -> dict:
        """Update an existing pull request comment.

        Args:
            repo: Full repository name (e.g. 'owner/repo').
            comment_id: The ID of the comment to update.
            body: New comment text.

        Returns:
            The GitHub API response dict.
        """
        url = f'{self.BASE_URL}/repos/{repo}/issues/comments/{comment_id}'
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                url, json={'body': body}, headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()
