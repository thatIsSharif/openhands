"""Tests for Jira automation service."""

import hashlib
import hmac
import json

import pytest

from integrations.automation.jira_automation_service import (
    compute_jira_event_id,
    extract_jira_issue_data,
    generate_jira_branch_name,
    verify_jira_signature,
)


class TestVerifyJiraSignature:
    def test_valid_signature(self):
        secret = 'test_secret'
        body = json.dumps({'event': 'test'}).encode()
        expected = hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        header = f'sha256={expected}'
        assert verify_jira_signature(body, header, secret) is True

    def test_invalid_signature(self):
        secret = 'test_secret'
        body = json.dumps({'event': 'test'}).encode()
        header = 'sha256=invalid_signature'
        assert verify_jira_signature(body, header, secret) is False

    def test_missing_header(self):
        body = json.dumps({'event': 'test'}).encode()
        assert verify_jira_signature(body, None, 'test_secret') is False

    def test_empty_signature(self):
        body = json.dumps({'event': 'test'}).encode()
        assert verify_jira_signature(body, '', 'test_secret') is False

    def test_signature_with_sha1_prefix(self):
        secret = 'test_secret'
        body = json.dumps({'event': 'test'}).encode()
        expected = hmac.new(
            secret.encode(), body, hashlib.sha1
        ).hexdigest()
        header = f'sha1={expected}'
        # SHA1 is not SHA256, so it should fail
        assert verify_jira_signature(body, header, secret) is False


class TestComputeJiraEventId:
    def test_deterministic_for_same_input(self):
        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
            'timestamp': 1000000,
        }
        id1 = compute_jira_event_id(payload)
        id2 = compute_jira_event_id(payload)
        assert id1 == id2

    def test_different_for_different_issues(self):
        payload1 = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
            'timestamp': 1000000,
        }
        payload2 = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '67890'},
            'timestamp': 1000000,
        }
        assert compute_jira_event_id(payload1) != compute_jira_event_id(payload2)

    def test_returns_string(self):
        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
        }
        event_id = compute_jira_event_id(payload)
        assert isinstance(event_id, str)
        assert len(event_id) == 64  # SHA256 hex digest


class TestExtractJiraIssueData:
    def test_extracts_all_fields(self):
        payload = {
            'issue': {
                'key': 'KAN-17',
                'fields': {
                    'summary': 'Test issue',
                    'description': 'Test description',
                    'issuetype': {'name': 'Story'},
                    'priority': {'name': 'Medium'},
                    'reporter': {'displayName': 'User1'},
                    'labels': ['automation'],
                },
            }
        }
        data = extract_jira_issue_data(payload)
        assert data is not None
        assert data['issue_key'] == 'KAN-17'
        assert data['summary'] == 'Test issue'
        assert data['description'] == 'Test description'
        assert data['issue_type'] == 'Story'
        assert data['priority'] == 'Medium'
        assert data['reporter'] == 'User1'
        assert data['labels'] == ['automation']

    def test_missing_issue_key(self):
        payload = {'issue': {'fields': {}}}
        assert extract_jira_issue_data(payload) is None

    def test_missing_optional_fields(self):
        payload = {
            'issue': {
                'key': 'KAN-17',
                'fields': {
                    'summary': 'Test',
                },
            }
        }
        data = extract_jira_issue_data(payload)
        assert data is not None
        assert data['description'] == ''
        assert data['issue_type'] == ''
        assert data['priority'] == ''
        assert data['labels'] == []

    def test_no_reporter(self):
        payload = {
            'issue': {
                'key': 'KAN-17',
                'fields': {
                    'summary': 'Test',
                    'reporter': None,
                },
            }
        }
        data = extract_jira_issue_data(payload)
        assert data is not None
        assert data['reporter'] == ''


class TestGenerateJiraBranchName:
    def test_feature_branch(self):
        branch = generate_jira_branch_name(
            'KAN-17', 'Story', 'Implement automation platform'
        )
        assert branch == 'feature/KAN-17-implement-automation-platform'

    def test_bugfix_branch(self):
        branch = generate_jira_branch_name(
            'KAN-17', 'Bug', 'Fix null pointer exception'
        )
        assert branch == 'bugfix/KAN-17-fix-null-pointer-exception'

    def test_slug_truncation(self):
        long_summary = 'a' * 100
        branch = generate_jira_branch_name('KAN-17', 'Story', long_summary)
        # Should not exceed reasonable length
        assert len(branch) < 100

    def test_special_characters_in_summary(self):
        branch = generate_jira_branch_name(
            'KAN-17', 'Story', 'Implement @#$%^& feature!'
        )
        assert 'feature' in branch
        # No special characters
        assert all(
            c.isalnum() or c in '-/' for c in branch
        ), f'Branch has invalid chars: {branch}'
