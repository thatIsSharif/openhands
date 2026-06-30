"""
Jira API utilities.

Usage:
    from app.utils.jira import add_comment

    add_comment("KAN-23", "Implementation complete.")

Requires env vars: JIRA_EMAIL, JIRA_API_KEY, and JIRA_DOMAIN or JIRA_URL.
"""

import base64
import json
import os
import urllib.error
import urllib.request


def _text_to_adf(body: str) -> dict:
    """Convert plain text to Atlassian Document Format."""
    lines = body.strip().split('\n')
    content = []
    for line in lines:
        content.append({
            'type': 'paragraph',
            'content': [
                {'type': 'text', 'text': line}
            ],
        })
    return {
        'type': 'doc',
        'version': 1,
        'content': content,
    }


def _get_auth() -> tuple[str, str, str]:
    """Get Jira auth credentials and domain, or raise ValueError."""
    email = os.environ.get('JIRA_EMAIL')
    api_key = os.environ.get('JIRA_API_KEY')
    domain = os.environ.get('JIRA_DOMAIN') or (
        os.environ.get('JIRA_URL', '')
        .replace('https://', '')
        .replace('http://', '')
        .split('/')[0]
    )

    missing = []
    if not email:
        missing.append('JIRA_EMAIL')
    if not api_key:
        missing.append('JIRA_API_KEY')
    if not domain:
        missing.append('JIRA_DOMAIN or JIRA_URL')
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    return email, api_key, domain


def _auth_headers(email: str, api_key: str) -> dict:
    encoded = base64.b64encode(f'{email}:{api_key}'.encode()).decode()
    return {
        'Authorization': f'Basic {encoded}',
        'Content-Type': 'application/json',
    }


def add_comment(issue_key: str, body: str) -> dict:
    """
    Post a comment to a Jira issue.

    Args:
        issue_key: Jira issue key (e.g. "KAN-23").
        body: Comment text (plain text, converted to ADF automatically).

    Returns:
        Response dict with at least 'id' key.

    Raises:
        ValueError: If required env vars are missing.
        RuntimeError: If the API call fails.
    """
    email, api_key, domain = _get_auth()
    url = f'https://{domain}/rest/api/3/issue/{issue_key}/comment'
    payload = json.dumps({'body': _text_to_adf(body)}).encode('utf-8')

    req = urllib.request.Request(
        url, data=payload, headers=_auth_headers(email, api_key),
    )

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f'Jira API error (HTTP {e.code}): {e.read().decode()}'
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'Jira connection error: {e.reason}') from e


def get_comments(issue_key: str) -> list[dict]:
    """List all comments on a Jira issue.

    Args:
        issue_key: Jira issue key (e.g. "KAN-23").

    Returns:
        List of comment dicts, each containing at least 'id' and 'body'.

    Raises:
        ValueError: If required env vars are missing.
        RuntimeError: If the API call fails.
    """
    email, api_key, domain = _get_auth()
    url = f'https://{domain}/rest/api/3/issue/{issue_key}/comment'

    req = urllib.request.Request(url, headers=_auth_headers(email, api_key))

    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return data.get('comments', [])
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f'Jira API error (HTTP {e.code}): {e.read().decode()}'
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'Jira connection error: {e.reason}') from e


def update_comment(issue_key: str, comment_id: str, body: str) -> dict:
    """Update an existing comment on a Jira issue.

    Args:
        issue_key: Jira issue key (e.g. "KAN-23").
        comment_id: The ID of the comment to update.
        body: New comment text (plain text, converted to ADF automatically).

    Returns:
        Response dict from the Jira API.

    Raises:
        ValueError: If required env vars are missing.
        RuntimeError: If the API call fails.
    """
    email, api_key, domain = _get_auth()
    url = (
        f'https://{domain}/rest/api/3/issue/{issue_key}/comment/{comment_id}'
    )
    payload = json.dumps({'body': _text_to_adf(body)}).encode('utf-8')

    req = urllib.request.Request(
        url, data=payload, headers=_auth_headers(email, api_key),
        method='PUT',
    )

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f'Jira API error (HTTP {e.code}): {e.read().decode()}'
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'Jira connection error: {e.reason}') from e


TOKEN_USAGE_MARKER = '*OpenHands Automation Complete*'


def add_or_update_token_usage_comment(issue_key: str, body: str) -> dict:
    """Post or update a token-usage comment on a Jira issue.

    Searches existing comments for one containing TOKEN_USAGE_MARKER.
    If found, updates it. Otherwise, creates a new comment.

    Args:
        issue_key: Jira issue key (e.g. "KAN-23").
        body: Comment text (plain text, converted to ADF automatically).

    Returns:
        Response dict from the Jira API, with at least 'id' key.

    Raises:
        ValueError: If required env vars are missing.
        RuntimeError: If the API call fails.
    """
    for comment in get_comments(issue_key):
        # Check rendered/plain-text body for our marker
        comment_body = comment.get('body', {})
        if isinstance(comment_body, dict):
            raw = json.dumps(comment_body)
        else:
            raw = str(comment_body)
        if TOKEN_USAGE_MARKER in raw:
            cid = comment.get('id')
            if cid:
                return update_comment(issue_key, cid, body)

    # No existing token-usage comment found, create new one
    return add_comment(issue_key, body)
