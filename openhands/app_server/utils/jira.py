"""
Jira API utilities.

Usage:
    from app.utils.jira import add_comment

    add_comment("KAN-23", "Implementation complete.")

Requires env vars: JIRA_EMAIL, JIRA_API_KEY, and JIRA_DOMAIN or JIRA_URL.
"""

import os
import json
import base64
import urllib.request
import urllib.error


def _text_to_adf(body: str) -> dict:
    """Convert plain text to Atlassian Document Format."""
    lines = body.strip().split("\n")
    content = []
    for line in lines:
        content.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": line}
            ],
        })
    return {
        "type": "doc",
        "version": 1,
        "content": content,
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
    email = os.environ.get("JIRA_EMAIL")
    api_key = os.environ.get("JIRA_API_KEY")
    domain = os.environ.get("JIRA_DOMAIN") or (
        os.environ.get("JIRA_URL", "")
        .replace("https://", "")
        .replace("http://", "")
        .split("/")[0]
    )

    print("***********************************************************************************************************")
    missing = []
    if not email:
        missing.append("JIRA_EMAIL")
    if not api_key:
        missing.append("JIRA_API_KEY")
    if not domain:
        missing.append("JIRA_DOMAIN or JIRA_URL")
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    url = f"https://{domain}/rest/api/3/issue/{issue_key}/comment"
    payload = json.dumps({"body": _text_to_adf(body)}).encode("utf-8")
    encoded_creds = base64.b64encode(f"{email}:{api_key}".encode()).decode()

    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Basic {encoded_creds}",
        "Content-Type": "application/json",
    })

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Jira API error (HTTP {e.code}): {e.read().decode()}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Jira connection error: {e.reason}") from e
