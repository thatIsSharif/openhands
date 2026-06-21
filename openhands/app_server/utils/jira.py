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
from urllib.parse import urlencode


def _get_credentials() -> tuple[str, str, str]:
    """Get Jira credentials from environment variables.

    Returns:
        Tuple of (email, api_key, domain).

    Raises:
        ValueError: If required env vars are missing.
    """
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


def _jira_request(
    method: str,
    path: str,
    data: bytes | None = None,
) -> dict:
    """Make an authenticated request to the Jira REST API.

    Args:
        method: HTTP method (GET, POST, PUT, etc.).
        path: API path starting with '/' (e.g. '/rest/api/3/issue/KAN-123').
        data: Optional request body bytes.

    Returns:
        Parsed JSON response dict.

    Raises:
        ValueError: If required env vars are missing.
        RuntimeError: If the API call fails.
    """
    email, api_key, domain = _get_credentials()
    encoded_creds = base64.b64encode(f'{email}:{api_key}'.encode()).decode()

    url = f'https://{domain}{path}'
    headers = {
        'Authorization': f'Basic {encoded_creds}',
        'Content-Type': 'application/json',
    }

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(
            f'Jira API error (HTTP {e.code}) for {method} {path}: {body}'
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'Jira connection error: {e.reason}') from e


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
    payload = json.dumps({'body': _text_to_adf(body)}).encode('utf-8')
    return _jira_request(
        'POST', f'/rest/api/3/issue/{issue_key}/comment', data=payload
    )


def fetch_issue(issue_key: str) -> dict:
    """Fetch issue details from Jira.

    Args:
        issue_key: Jira issue key (e.g. "KAN-123").

    Returns:
        Dict with keys: key, summary, description, issue_type, priority,
        status, reporter, labels, project_key, assignee, created_at,
        updated_at.

    Raises:
        ValueError: If required env vars are missing.
        RuntimeError: If the API call fails.
    """
    fields = (
        'summary,description,issuetype,priority,status,reporter,'
        'labels,project,assignee,created,updated'
    )
    raw = _jira_request(
        'GET', f'/rest/api/3/issue/{issue_key}?{urlencode({"fields": fields})}'
    )

    fields_data = raw.get('fields', {})

    def _safe_val(obj, *keys):
        """Safely traverse nested dicts."""
        for key in keys:
            if isinstance(obj, dict):
                obj = obj.get(key)
            else:
                return None
        return obj

    return {
        'key': raw.get('key', issue_key),
        'summary': fields_data.get('summary', ''),
        'description': _safe_val(fields_data, 'description', 'content')
        or fields_data.get('description', ''),
        'issue_type': _safe_val(fields_data, 'issuetype', 'name'),
        'priority': _safe_val(fields_data, 'priority', 'name'),
        'status': _safe_val(fields_data, 'status', 'name'),
        'reporter': _safe_val(fields_data, 'reporter', 'displayName'),
        'assignee': _safe_val(fields_data, 'assignee', 'displayName'),
        'labels': fields_data.get('labels', []),
        'project_key': _safe_val(fields_data, 'project', 'key'),
        'created_at': fields_data.get('created'),
        'updated_at': fields_data.get('updated'),
    }


def fetch_issue_comments(issue_key: str) -> list[dict]:
    """Fetch all comments from a Jira issue.

    Args:
        issue_key: Jira issue key (e.g. "KAN-123").

    Returns:
        List of comment dicts with keys: id, author, body, created, updated.

    Raises:
        ValueError: If required env vars are missing.
        RuntimeError: If the API call fails.
    """
    raw = _jira_request(
        'GET', f'/rest/api/3/issue/{issue_key}/comment'
    )

    comments = []
    for c in raw.get('comments', []):
        # Extract text content from ADF
        body_adf = c.get('body', {})
        text_parts = []
        for paragraph in body_adf.get('content', []):
            if paragraph.get('type') == 'paragraph':
                for text_node in paragraph.get('content', []):
                    if text_node.get('type') == 'text':
                        text_parts.append(text_node.get('text', ''))

        comments.append({
            'id': c.get('id'),
            'author': (c.get('author') or {}).get('displayName', ''),
            'body': '\n'.join(text_parts),
            'created': c.get('created'),
            'updated': c.get('updated'),
        })

    return comments


def transition_issue(issue_key: str, transition_id: str | int) -> dict:
    """Transition a Jira issue to a new status.

    Args:
        issue_key: Jira issue key (e.g. "KAN-123").
        transition_id: Transition ID (e.g. "11" for "In Progress").

    Returns:
        Response dict (typically empty on success).

    Raises:
        ValueError: If required env vars are missing.
        RuntimeError: If the API call fails.
    """
    payload = json.dumps({
        'transition': {'id': str(transition_id)},
    }).encode('utf-8')
    return _jira_request(
        'POST', f'/rest/api/3/issue/{issue_key}/transitions', data=payload
    )


def get_issue_repository(issue_key: str, custom_field_id: str | None = None) -> str | None:
    """Extract repository from a Jira issue.

    Checks custom fields first (most orgs store the repo in a
    custom field like ``customfield_10020``), then falls back to
    deriving from the project key.

    Args:
        issue_key: Jira issue key (e.g. "KAN-123").
        custom_field_id: Optional Jira custom field ID that may contain
            a repository reference (e.g. "customfield_10020").

    Returns:
        Repository name in ``owner/repo`` format, or ``None`` if not found.
    """
    raw = _jira_request('GET', f'/rest/api/3/issue/{issue_key}')

    fields = raw.get('fields', {})

    # Check custom field first
    if custom_field_id:
        repo_val = fields.get(custom_field_id)
        if repo_val:
            repo_str = str(repo_val)
            # Handle both "owner/repo" and just "repo" formats
            if '/' in repo_str:
                return repo_str
            # It might be just a repo name, we still return it
            return repo_str

    # Fall back to project key mapping via environment
    project_key = (fields.get('project') or {}).get('key', '')
    if project_key.lower() in os.environ.get('JIRA_PROJECT_REPOS', '').lower():
        import ast
        try:
            mapping = ast.literal_eval(os.environ['JIRA_PROJECT_REPOS'])
            return mapping.get(project_key)
        except (ValueError, SyntaxError):
            pass

    return None
