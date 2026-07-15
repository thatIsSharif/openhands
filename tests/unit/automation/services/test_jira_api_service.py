"""Tests for JiraApiService."""

from unittest.mock import patch

import pytest

from openhands.app_server.automation.services.jira_api_service import (
    JiraApiService,
)


@pytest.fixture
def jira_service():
    with patch.dict(
        'os.environ',
        {
            'JIRA_EMAIL': 'test@example.com',
            'JIRA_API_KEY': 'test-api-key',
            'JIRA_DOMAIN': 'test-domain.atlassian.net',
        },
    ):
        return JiraApiService()


def test_constructor_missing_config():
    """JiraApiService raises ValueError when required env vars are missing."""
    with patch.dict('os.environ', {}, clear=True):
        with pytest.raises(ValueError, match='Missing required Jira config'):
            JiraApiService()


def test_get_issue(jira_service):
    """get_issue calls the Jira API and returns issue data."""
    expected = {'key': 'KAN-23', 'fields': {'summary': 'Test issue'}}

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = mock_urlopen.return_value.__enter__.return_value
        import json

        mock_resp.read.return_value = json.dumps(expected).encode('utf-8')

        result = jira_service.get_issue('KAN-23')

    assert result == expected


def test_transition_issue(jira_service):
    """transition_issue finds and applies the correct transition."""
    transitions_response = {
        'transitions': [
            {'id': '2', 'to': {'name': 'In Progress'}},
            {'id': '3', 'to': {'name': 'Done'}},
        ]
    }

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = mock_urlopen.return_value.__enter__.return_value
        import json

        # First call returns transitions, second returns success
        mock_resp.read.side_effect = [
            json.dumps(transitions_response).encode('utf-8'),
            json.dumps({}).encode('utf-8'),
        ]

        result = jira_service.transition_issue('KAN-23', 'In Progress')

    assert result is True


def test_transition_issue_not_found(jira_service):
    """transition_issue raises RuntimeError when transition is not found."""
    transitions_response = {'transitions': [{'id': '3', 'to': {'name': 'Done'}}]}

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = mock_urlopen.return_value.__enter__.return_value
        import json

        mock_resp.read.return_value = json.dumps(transitions_response).encode('utf-8')

        with pytest.raises(RuntimeError, match='No transition to "In Progress"'):
            jira_service.transition_issue('KAN-23', 'In Progress')


def test_add_comment(jira_service):
    """add_comment posts a comment to Jira."""
    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = mock_urlopen.return_value.__enter__.return_value
        import json

        mock_resp.read.return_value = json.dumps({'id': '12345'}).encode('utf-8')

        result = jira_service.add_comment('KAN-23', 'Test comment')

    assert result['id'] == '12345'


def test_get_comments(jira_service):
    """get_comments returns the list of comments."""
    comments_response = {
        'comments': [
            {'id': '1', 'body': 'First comment'},
            {'id': '2', 'body': 'Second comment'},
        ]
    }

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = mock_urlopen.return_value.__enter__.return_value
        import json

        mock_resp.read.return_value = json.dumps(comments_response).encode('utf-8')

        result = jira_service.get_comments('KAN-23')

    assert len(result) == 2
    assert result[0]['id'] == '1'


def test_add_or_update_token_usage_comment_new(jira_service):
    """Token usage comment is created when no existing marker is found."""
    comments_response = {'comments': [{'id': '1', 'body': {'type': 'doc'}}]}

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = mock_urlopen.return_value.__enter__.return_value
        import json

        # First call returns comments (empty), second creates new comment
        mock_resp.read.side_effect = [
            json.dumps(comments_response).encode('utf-8'),
            json.dumps({'id': '999'}).encode('utf-8'),
        ]

        result = jira_service.add_or_update_token_usage_comment(
            'KAN-23', 'Token usage data'
        )

    assert result['id'] == '999'


def test_add_or_update_token_usage_comment_update(jira_service):
    """Token usage comment is updated when existing marker is found."""
    comments_response = {
        'comments': [
            {
                'id': '42',
                'body': {
                    'content': [
                        {
                            'content': [
                                {
                                    'text': 'OpenHands Automation Complete',
                                    'type': 'text',
                                }
                            ],
                            'type': 'paragraph',
                        }
                    ],
                    'type': 'doc',
                    'version': 1,
                },
            }
        ]
    }

    with patch('urllib.request.urlopen') as mock_urlopen:
        mock_resp = mock_urlopen.return_value.__enter__.return_value
        import json

        mock_resp.read.side_effect = [
            json.dumps(comments_response).encode('utf-8'),
            json.dumps({'id': '42'}).encode('utf-8'),
        ]

        result = jira_service.add_or_update_token_usage_comment(
            'KAN-23', 'Updated token data'
        )

    assert result['id'] == '42'
