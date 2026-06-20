"""
GitHub API utilities for automation workflows.

Provides simple HTTP-based functions for posting comments on PRs
via the GitHub REST API. Uses GITHUB_TOKEN for authentication to
keep dependencies minimal (stdlib urllib only).

Usage:
    from openhands.app_server.utils.github import add_pr_comment
    add_pr_comment("owner/repo", 42, "Addressed review feedback.")

Requires env var: GITHUB_TOKEN
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def add_pr_comment(repository: str, pr_number: int, body: str) -> dict:
    """Post a comment on a GitHub pull request.

    Args:
        repository: Repository full name (e.g. "owner/repo").
        pr_number: Pull request number.
        body: Comment text.

    Returns:
        Response dict with at least 'id' key.

    Raises:
        ValueError: If GITHUB_TOKEN is not set.
        RuntimeError: If the API call fails.
    """
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        raise ValueError('Missing required env var: GITHUB_TOKEN')

    url = (
        f'https://api.github.com/repos/{repository}/issues/{pr_number}/comments'
    )
    payload = json.dumps({'body': body}).encode('utf-8')

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json',
        },
    )

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f'GitHub API error (HTTP {e.code}): {e.read().decode()}'
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'GitHub connection error: {e.reason}') from e
