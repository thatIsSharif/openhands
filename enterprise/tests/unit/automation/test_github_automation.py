"""Tests for GitHub automation service."""

import hashlib
import hmac
import json

from integrations.automation.github_automation_service import (
    compute_github_event_id,
    extract_github_review_data,
    verify_github_signature,
)


class TestVerifyGithubSignature:
    def test_valid_signature(self):
        secret = 'test_secret'
        body = json.dumps({'event': 'test'}).encode()
        expected = hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        header = f'sha256={expected}'
        assert verify_github_signature(body, header, secret) is True

    def test_invalid_signature(self):
        secret = 'test_secret'
        body = json.dumps({'event': 'test'}).encode()
        header = 'sha256=invalid_signature'
        assert verify_github_signature(body, header, secret) is False

    def test_missing_header(self):
        body = json.dumps({'event': 'test'}).encode()
        assert verify_github_signature(body, None, 'test_secret') is False

    def test_empty_string(self):
        body = json.dumps({'event': 'test'}).encode()
        assert verify_github_signature(body, '', 'test_secret') is False


class TestComputeGithubEventId:
    def test_deterministic_for_same_input(self):
        payload = {'comment': {'id': 12345}}
        id1 = compute_github_event_id(payload, 'delivery-abc')
        id2 = compute_github_event_id(payload, 'delivery-abc')
        assert id1 == id2

    def test_different_for_different_deliveries(self):
        payload = {'comment': {'id': 12345}}
        assert compute_github_event_id(
            payload, 'delivery-abc'
        ) != compute_github_event_id(payload, 'delivery-xyz')

    def test_returns_sha256_hexdigest(self):
        event_id = compute_github_event_id(
            {'comment': {'id': 12345}}, 'delivery-abc'
        )
        assert isinstance(event_id, str)
        assert len(event_id) == 64


class TestExtractGithubReviewData:
    def test_extracts_all_fields(self):
        payload = {
            'action': 'created',
            'repository': {
                'full_name': 'owner/repo',
                'owner': {'login': 'owner'},
                'name': 'repo',
            },
            'pull_request': {
                'number': 42,
                'head': {'ref': 'feature-branch', 'sha': 'abc123'},
                'base': {'ref': 'main', 'sha': 'def456'},
                'title': 'Test PR',
                'body': 'PR description',
            },
            'comment': {
                'id': 98765,
                'body': 'Please fix this issue',
            },
            'sender': {
                'login': 'reviewer1',
            },
        }
        data = extract_github_review_data(payload)
        assert data is not None
        assert data['repository'] == 'owner/repo'
        assert data['owner'] == 'owner'
        assert data['pr_number'] == 42
        assert data['branch'] == 'feature-branch'
        assert data['pr_title'] == 'Test PR'
        assert data['pr_body'] == 'PR description'
        assert data['review_comment'] == 'Please fix this issue'
        assert data['reviewer'] == 'reviewer1'
        assert data['comment_id'] == 98765

    def test_missing_required_fields(self):
        payload = {'repository': {}, 'pull_request': {}, 'comment': {}, 'sender': {}}
        data = extract_github_review_data(payload)
        assert data is None

    def test_no_pr_body(self):
        payload = {
            'action': 'created',
            'repository': {
                'full_name': 'owner/repo',
                'owner': {'login': 'owner'},
                'name': 'repo',
            },
            'pull_request': {
                'number': 42,
                'head': {'ref': 'feature-branch'},
                'title': 'Test PR',
            },
            'comment': {
                'id': 98765,
                'body': 'Review comment',
            },
            'sender': {'login': 'reviewer1'},
        }
        data = extract_github_review_data(payload)
        assert data is not None
        assert data['pr_body'] == ''
