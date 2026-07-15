"""
GitHub API utilities for automation workflows.

Provides simple HTTP-based functions for posting comments on PRs
via the GitHub REST API. Uses the GitHub App installation token
(via GitHubAppTokenManager) when available, falling back to the
GITHUB_TOKEN environment variable.

Handles staleness: on HTTP 401/403 the token is refreshed once and
the request is retried automatically.

Usage:
    from openhands.app_server.utils.github import add_pr_comment
    add_pr_comment("owner/repo", 42, "Addressed review feedback.")

Requires env vars: GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY (recommended)
    or GITHUB_TOKEN (legacy fallback).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def _resolve_token(repository: str) -> str:
    """Resolve a GitHub API token for the given repository.

    Tries, in order:
    1. GitHub App installation token (via GitHubAppTokenManager)
    2. ``GITHUB_TOKEN`` environment variable (legacy PAT / injected token)

    Args:
        repository: Repository full name (e.g. ``"owner/repo"``).

    Returns:
        A bearer token string.

    Raises:
        ValueError: If no token source is available.
    """
    from openhands.app_server.utils.github_app import (
        GitHubAppNotConfiguredError,
        GitHubAppTokenManager,
    )

    owner, _, repo_name = repository.partition('/')

    # 1. GitHub App token manager (preferred — auto-refreshing)
    if GitHubAppTokenManager.is_available():
        try:
            return GitHubAppTokenManager.get_token_for_repository(
                owner, repo_name
            )
        except GitHubAppNotConfiguredError:
            pass  # fall through
        except Exception:
            logger.exception(
                'GitHub App token resolution failed for %s, '
                'falling back to GITHUB_TOKEN env',
                repository,
            )

    # 2. Environment variable fallback
    token = os.environ.get('GITHUB_TOKEN', '')
    if token:
        return token

    raise ValueError(
        'No GitHub token available. Configure GitHub App '
        '(GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY) or set GITHUB_TOKEN.'
    )


def _refresh_and_retry(repository: str) -> str:
    """Force-refresh the GitHub App token for the given repository.

    Called after a 401/403 response.  Falls back to the env var if
    GitHub App is not configured.

    Returns:
        A fresh token string.
    """
    from openhands.app_server.utils.github_app import (
        GitHubAppTokenManager,
    )

    owner, _, repo_name = repository.partition('/')

    if GitHubAppTokenManager.is_available():
        try:
            return GitHubAppTokenManager.refresh_token(owner, repo_name)
        except Exception:
            logger.exception(
                'Token refresh failed for %s', repository,
            )

    # Fallback: re-read the env var (may have been updated externally)
    token = os.environ.get('GITHUB_TOKEN', '')
    if token:
        return token

    raise ValueError('No GitHub token available after refresh attempt.')


def add_pr_comment(repository: str, pr_number: int, body: str) -> dict:
    """Post a comment on a GitHub pull request.

    Uses GitHub App installation token when available, falling back to
    ``GITHUB_TOKEN`` env var.  Automatically retries once on 401/403
    with a freshly-minted token.

    Args:
        repository: Repository full name (e.g. ``"owner/repo"``).
        pr_number: Pull request number.
        body: Comment text.

    Returns:
        Response dict with at least ``'id'`` key.

    Raises:
        ValueError: If no token source is available.
        RuntimeError: If the API call fails after retry.
    """
    token = _resolve_token(repository)

    url = (
        f'https://api.github.com/repos/{repository}/issues/{pr_number}/comments'
    )
    payload = json.dumps({'body': body}).encode('utf-8')

    def _make_request( bearer_token: str) -> dict:
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

        # Retry once on 401/403 (token may have expired mid-flight)
        if status in (401, 403):
            logger.info(
                'GitHub API returned %s for %s PR #%s, '
                'refreshing token and retrying once',
                status, repository, pr_number,
            )
            try:
                fresh_token = _refresh_and_retry(repository)
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
