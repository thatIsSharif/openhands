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
from datetime import datetime, timezone


def _format_ts(ts: str | None) -> str:
    """Format an ISO-8601 timestamp for display."""
    if not ts:
        return 'N/A'
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
    except (ValueError, AttributeError):
        return ts


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


# ── Token-usage comment helpers (used by jira_webhook_router) ──────────────


def _get_agent_url_from_sandbox(sandbox) -> str | None:
    """Extract the agent server URL from a sandbox's exposed URLs."""
    from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER

    for exposed_url in (sandbox.exposed_urls or []):
        if exposed_url.name == AGENT_SERVER:
            return exposed_url.url
    return None


async def fetch_live_agent_metrics(
    agent_server_url: str,
    conversation_id: str,
    session_api_key: str,
    httpx_client,
) -> dict:
    """Fetch live conversation metrics from the agent server API.

    Returns a dict with keys: accumulated_cost, model_name, prompt_tokens,
    completion_tokens, cache_read_tokens, cache_write_tokens,
    reasoning_tokens.

    On failure returns an empty dict (callers should fall back to DB).
    """
    from openhands.app_server.utils.docker_utils import (
        replace_localhost_hostname_for_docker,
    )

    url = replace_localhost_hostname_for_docker(agent_server_url)
    url = f'{url}/api/conversations/{conversation_id}'

    try:
        resp = await httpx_client.get(
            url,
            headers={'X-Session-API-Key': session_api_key},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return {}

        data = resp.json()
        stats = data.get('stats') or {}
        usage_to_metrics = stats.get('usage_to_metrics') or {}
        agent = usage_to_metrics.get('agent') or {}
        usage = agent.get('accumulated_token_usage') or {}

        return {
            'accumulated_cost': agent.get('accumulated_cost', 0.0),
            'model_name': agent.get('model_name', 'default'),
            'prompt_tokens': usage.get('prompt_tokens', 0),
            'completion_tokens': usage.get('completion_tokens', 0),
            'cache_read_tokens': usage.get('cache_read_tokens', 0),
            'cache_write_tokens': usage.get('cache_write_tokens', 0),
            'reasoning_tokens': usage.get('reasoning_tokens', 0),
            'created_at': data.get('created_at'),
            'updated_at': data.get('updated_at'),
        }
    except Exception:
        import traceback

        from openhands.app_server.utils.logger import (
            openhands_logger as logger,
        )

        logger.error(
            f'[Automation] Error fetching live conversation '
            f'{conversation_id}: {traceback.format_exc()}'
        )
        return {}


def build_token_usage_comment(
    accumulated_cost: float = 0.0,
    model_name: str = 'default',
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    max_budget: float | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> str:
    """Build a beautified token-usage comment for a Jira issue.

    Includes emojis, formatted token counts, cost, timestamps,
    and budget usage.
    """
    lines = [
        '🎯 *OpenHands Automation Complete*',
        '',
        f'💰 *Total Cost:* ${accumulated_cost:.6f}',
        f'🤖 *Model:* {model_name}',
    ]

    total_tokens = prompt_tokens + completion_tokens
    if total_tokens > 0 or prompt_tokens > 0 or completion_tokens > 0:
        lines.append('')
        lines.append('📊 *Token Usage:*')
        lines.append(f'   • Prompt tokens:     {prompt_tokens:>10,}')
        lines.append(f'   • Completion tokens: {completion_tokens:>10,}')
        lines.append(f'   • Total tokens:      {total_tokens:>10,}')
        if cache_read_tokens:
            lines.append(
                f'   • Cache read tokens:  {cache_read_tokens:>10,} 💾'
            )
        if cache_write_tokens:
            lines.append(
                f'   • Cache write tokens: {cache_write_tokens:>10,} 💾'
            )
        if reasoning_tokens:
            lines.append(
                f'   • Reasoning tokens:   {reasoning_tokens:>10,} 🧠'
            )

    if max_budget and max_budget > 0:
        pct = accumulated_cost / max_budget * 100
        lines.append('')
        lines.append(
            f'📋 *Budget Usage:* ${accumulated_cost:.4f}'
            f' / ${max_budget:.4f} ({pct:.1f}%)'
        )

    lines.append('')
    lines.append(
        f'⏱️ *Created:* {_format_ts(created_at)}'
    )
    lines.append(
        f'⏱️ *Updated:* {_format_ts(updated_at)}'
    )

    return '\n'.join(lines)
