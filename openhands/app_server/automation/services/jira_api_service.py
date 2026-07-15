"""Jira API service for the automation platform.

Handles all deterministic Jira operations (issue loading, transitions,
comments, token usage) that were previously handled by the LLM.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request


class JiraApiService:
    """Concrete service for Jira REST API operations.

    Uses synchronous urllib calls (matching the existing pattern in
    openhands/app_server/utils/jira.py) authenticated via
    JIRA_EMAIL, JIRA_API_KEY, and JIRA_DOMAIN/JIRA_URL env vars.
    """

    def __init__(
        self,
        email: str | None = None,
        api_key: str | None = None,
        domain: str | None = None,
    ) -> None:
        self._email = email or os.environ.get('JIRA_EMAIL', '')
        self._api_key = api_key or os.environ.get('JIRA_API_KEY', '')
        domain_raw = domain or os.environ.get(
            'JIRA_DOMAIN',
            os.environ.get('JIRA_URL', ''),
        )
        # Strip protocol prefix if JIRA_URL was used
        self._domain = (
            domain_raw.replace('https://', '').replace('http://', '').split('/')[0]
        )

        missing = []
        if not self._email:
            missing.append('JIRA_EMAIL')
        if not self._api_key:
            missing.append('JIRA_API_KEY')
        if not self._domain:
            missing.append('JIRA_DOMAIN or JIRA_URL')
        if missing:
            raise ValueError(
                f'Missing required Jira config: {", ".join(missing)}'
            )

    def _auth_headers(self) -> dict[str, str]:
        encoded = base64.b64encode(
            f'{self._email}:{self._api_key}'.encode()
        ).decode()
        return {
            'Authorization': f'Basic {encoded}',
            'Content-Type': 'application/json',
        }

    def _request(
        self,
        method: str,
        path: str,
        data: dict | None = None,
    ) -> dict:
        """Make a Jira REST API request.

        Args:
            method: HTTP method (GET, POST, PUT).
            path: API path (e.g. '/rest/api/3/issue/KAN-23/comment').
            data: Optional JSON-serializable request body.

        Returns:
            Parsed JSON response dict.

        Raises:
            ValueError: If credentials are missing.
            RuntimeError: If the API call fails.
        """
        url = f'https://{self._domain}{path}'
        headers = self._auth_headers()
        body = json.dumps(data).encode('utf-8') if data else None

        req = urllib.request.Request(
            url, data=body, headers=headers, method=method,
        )

        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f'Jira API error (HTTP {e.code}): {e.read().decode()}'
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f'Jira connection error: {e.reason}'
            ) from e

    # ── Issue Operations ──────────────────────────────────────────────

    def get_issue(self, issue_key: str) -> dict:
        """Fetch issue details from Jira.

        Args:
            issue_key: Jira issue key (e.g. 'KAN-23').

        Returns:
            The full issue JSON from the Jira API.
        """
        return self._request(
            'GET', f'/rest/api/3/issue/{issue_key}'
        )

    def transition_issue(self, issue_key: str, target_state: str) -> bool:
        """Transition a Jira issue to a target state.

        Uses the transition name (e.g. 'In Progress') to find the
        matching transition ID from available transitions.

        Args:
            issue_key: Jira issue key (e.g. 'KAN-23').
            target_state: Target state name (e.g. 'In Progress').

        Returns:
            True if the transition succeeded.

        Raises:
            RuntimeError: If the transition cannot be found or applied.
        """
        # Fetch available transitions
        transitions_data = self._request(
            'GET',
            f'/rest/api/3/issue/{issue_key}/transitions',
        )
        available = transitions_data.get('transitions', [])

        # Find matching transition
        transition_id: str | None = None
        for t in available:
            if t.get('to', {}).get('name', '').lower() == target_state.lower():
                transition_id = t.get('id')
                break

        if not transition_id:
            available_names = [
                t.get('to', {}).get('name', '?')
                for t in available
            ]
            raise RuntimeError(
                f'No transition to "{target_state}" found for '
                f'{issue_key}. Available: {available_names}'
            )

        self._request(
            'POST',
            f'/rest/api/3/issue/{issue_key}/transitions',
            data={'transition': {'id': transition_id}},
        )
        return True

    # ── Comment Operations ────────────────────────────────────────────

    def _text_to_adf(self, body: str) -> dict:
        """Convert plain text to Atlassian Document Format."""
        lines = [line for line in body.strip().split('\n')]
        content = []
        for line in lines:
            if not line.strip():
                continue
            content.append({
                'type': 'paragraph',
                'content': [{'type': 'text', 'text': line}],
            })
        return {
            'type': 'doc',
            'version': 1,
            'content': content or [{'type': 'paragraph', 'content': []}],
        }

    def add_comment(self, issue_key: str, body: str | dict) -> dict:
        """Post a comment to a Jira issue.

        Args:
            issue_key: Jira issue key (e.g. 'KAN-23').
            body: Plain text or pre-built ADF dict.

        Returns:
            Response dict with at least 'id' key.
        """
        adf_body = body if isinstance(body, dict) else self._text_to_adf(body)
        return self._request(
            'POST',
            f'/rest/api/3/issue/{issue_key}/comment',
            data={'body': adf_body},
        )

    def get_comments(self, issue_key: str) -> list[dict]:
        """List all comments on a Jira issue."""
        data = self._request(
            'GET',
            f'/rest/api/3/issue/{issue_key}/comment',
        )
        return data.get('comments', [])

    def update_comment(
        self, issue_key: str, comment_id: str, body: str | dict
    ) -> dict:
        """Update an existing comment on a Jira issue."""
        adf_body = body if isinstance(body, dict) else self._text_to_adf(body)
        return self._request(
            'PUT',
            f'/rest/api/3/issue/{issue_key}/comment/{comment_id}',
            data={'body': adf_body},
        )

    TOKEN_USAGE_MARKER = 'OpenHands Automation Complete'

    def add_or_update_token_usage_comment(
        self, issue_key: str, body: str | dict
    ) -> dict:
        """Post or update a token-usage comment on a Jira issue.

        Searches existing comments for one containing TOKEN_USAGE_MARKER.
        If found, updates it. Otherwise, creates a new comment.
        """
        for comment in self.get_comments(issue_key):
            comment_body = comment.get('body', {})
            if isinstance(comment_body, dict):
                raw = json.dumps(comment_body)
            else:
                raw = str(comment_body)
            if self.TOKEN_USAGE_MARKER in raw:
                cid = comment.get('id')
                if cid:
                    return self.update_comment(issue_key, cid, body)

        return self.add_comment(issue_key, body)
