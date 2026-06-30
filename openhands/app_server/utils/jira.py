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
    """Convert plain text to Atlassian Document Format.

    Blank lines are collapsed instead of being turned into their own
    empty paragraph nodes, which is what was causing the oversized gaps
    between lines when rendered in Jira.
    """
    lines = [line for line in body.strip().split('\n')]
    content = []
    for line in lines:
        if not line.strip():
            continue
        content.append({
            'type': 'paragraph',
            'content': [
                {'type': 'text', 'text': line}
            ],
        })
    return {
        'type': 'doc',
        'version': 1,
        'content': content or [{'type': 'paragraph', 'content': []}],
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


def add_comment(issue_key: str, body) -> dict:
    """
    Post a comment to a Jira issue.

    Args:
        issue_key: Jira issue key (e.g. "KAN-23").
        body: Comment text (plain text, converted to ADF automatically),
            OR a pre-built ADF dict (e.g. from build_token_usage_comment).

    Returns:
        Response dict with at least 'id' key.

    Raises:
        ValueError: If required env vars are missing.
        RuntimeError: If the API call fails.
    """
    email, api_key, domain = _get_auth()
    url = f'https://{domain}/rest/api/3/issue/{issue_key}/comment'
    adf_body = body if isinstance(body, dict) else _text_to_adf(body)
    payload = json.dumps({'body': adf_body}).encode('utf-8')

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


def update_comment(issue_key: str, comment_id: str, body) -> dict:
    """Update an existing comment on a Jira issue.

    Args:
        issue_key: Jira issue key (e.g. "KAN-23").
        comment_id: The ID of the comment to update.
        body: New comment text (plain text, converted to ADF automatically),
            OR a pre-built ADF dict.

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
    adf_body = body if isinstance(body, dict) else _text_to_adf(body)
    payload = json.dumps({'body': adf_body}).encode('utf-8')

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


TOKEN_USAGE_MARKER = 'OpenHands Automation Complete'


def add_or_update_token_usage_comment(issue_key: str, body) -> dict:
    """Post or update a token-usage comment on a Jira issue.

    Searches existing comments for one containing TOKEN_USAGE_MARKER.
    If found, updates it. Otherwise, creates a new comment.

    Args:
        issue_key: Jira issue key (e.g. "KAN-23").
        body: Comment text (plain text) or a pre-built ADF dict, as
            returned by build_token_usage_comment().

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


def _text(value: str, *, bold: bool = False, color: str | None = None) -> dict:
    """Build an ADF text node, optionally bold and/or colored."""
    marks = []
    if bold:
        marks.append({'type': 'strong'})
    if color:
        marks.append({'type': 'textColor', 'attrs': {'color': color}})
    node: dict = {'type': 'text', 'text': value}
    if marks:
        node['marks'] = marks
    return node


def _stat_paragraph(label: str, value: str, *, value_color: str | None = None) -> dict:
    """A single 'Label: Value' line with the label muted and value emphasized."""
    return {
        'type': 'paragraph',
        'content': [
            _text(f'{label}: ', color='#6B778C'),
            _text(value, bold=True, color=value_color),
        ],
    }


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
) -> dict:
    """Build a token-usage comment for a Jira issue as a native ADF document.

    Uses real ADF nodes (heading, panel, bullet list, rule) instead of
    plain-text lines, so the comment renders as a clean, well-spaced card
    in Jira rather than a stack of loosely separated lines.

    Returns:
        An ADF document dict, ready to pass straight to add_comment(),
        update_comment(), or add_or_update_token_usage_comment().
    """
    content: list[dict] = []

    # Header
    content.append({
        'type': 'heading',
        'attrs': {'level': 3},
        'content': [
            {'type': 'emoji', 'attrs': {'shortName': ':dart:', 'text': '🎯'}},
            _text(f'  {TOKEN_USAGE_MARKER}', bold=True),
        ],
    })

    # Cost + model summary line
    total_tokens = prompt_tokens + completion_tokens
    summary_content = [
        _text('💰 Cost  '),
        _text(f'${accumulated_cost:.6f}', bold=True),
        _text('     🤖 Model  '),
        _text(model_name, bold=True),
    ]
    content.append({'type': 'paragraph', 'content': summary_content})

    content.append({'type': 'rule'})

    # Token usage section
    if total_tokens > 0 or prompt_tokens > 0 or completion_tokens > 0:
        content.append({
            'type': 'paragraph',
            'content': [_text('📊 Token Usage', bold=True)],
        })

        bullet_items = [
            ('Prompt tokens', f'{prompt_tokens:,}'),
            ('Completion tokens', f'{completion_tokens:,}'),
            ('Total tokens', f'{total_tokens:,}'),
        ]
        if cache_read_tokens:
            bullet_items.append(('Cache read tokens', f'{cache_read_tokens:,}'))
        if cache_write_tokens:
            bullet_items.append(('Cache write tokens', f'{cache_write_tokens:,}'))
        if reasoning_tokens:
            bullet_items.append(('Reasoning tokens', f'{reasoning_tokens:,}'))

        content.append({
            'type': 'bulletList',
            'content': [
                {
                    'type': 'listItem',
                    'content': [
                        {
                            'type': 'paragraph',
                            'content': [
                                _text(f'{label}: ', color='#6B778C'),
                                _text(value, bold=True),
                            ],
                        }
                    ],
                }
                for label, value in bullet_items
            ],
        })

    # Budget usage
    if max_budget and max_budget > 0:
        pct = accumulated_cost / max_budget * 100
        budget_color = '#DE350B' if pct >= 90 else (
            '#FF8B00' if pct >= 70 else '#00875A'
        )
        content.append({'type': 'rule'})
        content.append(_stat_paragraph(
            '📋 Budget Usage',
            f'${accumulated_cost:.4f} / ${max_budget:.4f}  ({pct:.1f}%)',
            value_color=budget_color,
        ))

    # Timestamps
    content.append({'type': 'rule'})
    content.append({
        'type': 'paragraph',
        'content': [
            _text('⏱️ Created  ', color='#6B778C'),
            _text(_format_ts(created_at), bold=True),
            _text('     ⏱️ Updated  ', color='#6B778C'),
            _text(_format_ts(updated_at), bold=True),
        ],
    })

    return {
        'type': 'doc',
        'version': 1,
        'content': content,
    }
