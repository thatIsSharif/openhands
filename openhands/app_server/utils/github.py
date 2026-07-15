"""
GitHub API utilities for automation workflows.

Provides simple HTTP-based functions for posting comments on PRs
via the GitHub REST API. Uses the GitHub App installation token
(via GitHubAppTokenManager) exclusively — no PAT fallback.

Usage:
    from openhands.app_server.utils.github import add_pr_comment
    add_pr_comment("owner/repo", 42, "Addressed review feedback.")

Requires env vars: GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, and
GITHUB_APP_INSTALLATION_ID.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from openhands.app_server.utils.github_app import GitHubAppTokenManager

logger = logging.getLogger(__name__)


def _resolve_token() -> str:
    """Resolve a GitHub App installation token.

    Reads ``GITHUB_APP_INSTALLATION_ID`` from the environment.

    Returns:
        A valid installation access token.

    Raises:
        GitHubAppNotConfiguredError: If GitHub App is not configured.
    """
    return GitHubAppTokenManager.get_token_for_installation()


def add_pr_comment(repository: str, pr_number: int, body: str) -> dict:
    """Post a comment on a GitHub pull request.

    Uses GitHub App installation token exclusively.  Automatically
    retries once on 401/403 with a freshly-minted token.

    Args:
        repository: Repository full name (e.g. ``"owner/repo"``).
        pr_number: Pull request number.
        body: Comment text.

    Returns:
        Response dict with at least ``'id'`` key.

    Raises:
        GitHubAppNotConfiguredError: If GitHub App is not configured.
        RuntimeError: If the API call fails after retry.
    """
    token = _resolve_token()

    url = (
        f'https://api.github.com/repos/{repository}/issues/{pr_number}/comments'
    )
    payload = json.dumps({'body': body}).encode('utf-8')

    def _make_request(bearer_token: str) -> dict:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                'Authorization': f'Bearer {bearer_token}',
                'Accept': 'application/vnd.github.v3+json',
                'Content-Type': 'application/json',
            },
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())

    try:
        return _make_request(token)
    except urllib.error.HTTPError as e:
        status = e.code
        body_text = e.read().decode()

        # Retry once on 401/403 (token may have expired)
        if status in (401, 403):
            logger.info(
                'GitHub API returned %s for %s PR #%s, '
                'refreshing token and retrying once',
                status, repository, pr_number,
            )
            try:
                fresh_token = GitHubAppTokenManager.refresh_installation_token()
                return _make_request(fresh_token)
            except urllib.error.HTTPError as e2:
                raise RuntimeError(
                    f'GitHub API error (HTTP {e2.code}) after token refresh: '
                    f'{e2.read().decode()}'
                ) from e2

        raise RuntimeError(
            f'GitHub API error (HTTP {status}): {body_text}'
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'GitHub connection error: {e.reason}') from e
