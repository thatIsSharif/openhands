"""Tests for AwsEventService.

This module tests the AWS S3-based implementation of EventService,
focusing on search functionality and S3 operations.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import botocore.exceptions
import pytest

from openhands.app_server.event.aws_event_service import (
    AwsEventService,
    AwsEventServiceInjector,
)
from openhands.app_server.file_store.s3 import (
    _ensure_url_scheme,
    _use_real_aws,
    create_s3_client,
)
from openhands.sdk.event import PauseEvent, TokenEvent


@pytest.fixture
def mock_s3_client():
    """Create a mock S3 client."""
    return MagicMock()


@pytest.fixture
def service(mock_s3_client) -> AwsEventService:
    """Create an AwsEventService instance for testing."""
    return AwsEventService(
        prefix=Path('users'),
        user_id='test_user',
        app_conversation_info_service=None,
        s3_client=mock_s3_client,
        bucket_name='test-bucket',
        app_conversation_info_load_tasks={},
    )


@pytest.fixture
def service_no_user(mock_s3_client) -> AwsEventService:
    """Create an AwsEventService instance without user_id."""
    return AwsEventService(
        prefix=Path('users'),
        user_id=None,
        app_conversation_info_service=None,
        s3_client=mock_s3_client,
        bucket_name='test-bucket',
        app_conversation_info_load_tasks={},
    )


def create_token_event() -> TokenEvent:
    """Helper to create a TokenEvent for testing."""
    return TokenEvent(
        source='agent', prompt_token_ids=[1, 2], response_token_ids=[3, 4]
    )


def create_pause_event() -> PauseEvent:
    """Helper to create a PauseEvent for testing."""
    return PauseEvent(source='user')


class TestAwsEventServiceLoadEvent:
    """Test cases for _load_event method."""

    def test_load_event_success(self, service: AwsEventService, mock_s3_client):
        """Test that _load_event successfully loads an event from S3."""
        event = create_token_event()
        json_data = event.model_dump_json()

        # Mock the S3 response
        mock_body = MagicMock()
        mock_body.read.return_value = json_data.encode('utf-8')
        mock_body.__enter__ = MagicMock(return_value=mock_body)
        mock_body.__exit__ = MagicMock(return_value=False)
        mock_s3_client.get_object.return_value = {'Body': mock_body}

        result = service._load_event(Path('some/path/event.json'))

        assert result is not None
        assert result.kind == 'TokenEvent'
        mock_s3_client.get_object.assert_called_once_with(
            Bucket='test-bucket', Key='some/path/event.json'
        )

    def test_load_event_not_found(self, service: AwsEventService, mock_s3_client):
        """Test that _load_event returns None when event doesn't exist."""
        error_response = {'Error': {'Code': 'NoSuchKey', 'Message': 'Not found'}}
        mock_s3_client.get_object.side_effect = botocore.exceptions.ClientError(
            error_response, 'GetObject'
        )

        result = service._load_event(Path('some/path/missing.json'))

        assert result is None

    def test_load_event_other_error(self, service: AwsEventService, mock_s3_client):
        """Test that _load_event returns None and logs error on other S3 errors."""
        error_response = {'Error': {'Code': 'AccessDenied', 'Message': 'Access denied'}}
        mock_s3_client.get_object.side_effect = botocore.exceptions.ClientError(
            error_response, 'GetObject'
        )

        result = service._load_event(Path('some/path/denied.json'))

        assert result is None


class TestAwsEventServiceStoreEvent:
    """Test cases for _store_event method."""

    def test_store_event_success(self, service: AwsEventService, mock_s3_client):
        """Test that _store_event successfully stores an event to S3."""
        event = create_token_event()

        service._store_event(Path('some/path/event.json'), event)

        mock_s3_client.put_object.assert_called_once()
        call_args = mock_s3_client.put_object.call_args
        assert call_args.kwargs['Bucket'] == 'test-bucket'
        assert call_args.kwargs['Key'] == 'some/path/event.json'
        # Verify the body is valid JSON
        body = call_args.kwargs['Body'].decode('utf-8')
        data = json.loads(body)
        assert data['kind'] == 'TokenEvent'


class TestAwsEventServiceSearchPaths:
    """Test cases for _search_paths method."""

    def test_search_paths_returns_paths(self, service: AwsEventService, mock_s3_client):
        """Test that _search_paths returns paths from S3."""
        mock_s3_client.list_objects_v2.return_value = {
            'Contents': [
                {'Key': 'users/test_user/v1_conversations/abc123/event1.json'},
                {'Key': 'users/test_user/v1_conversations/abc123/event2.json'},
            ]
        }

        result = service._search_paths(Path('users/test_user/v1_conversations/abc123'))

        assert len(result) == 2
        assert result[0] == Path('users/test_user/v1_conversations/abc123/event1.json')
        assert result[1] == Path('users/test_user/v1_conversations/abc123/event2.json')

    def test_search_paths_empty_bucket(self, service: AwsEventService, mock_s3_client):
        """Test that _search_paths handles empty results."""
        mock_s3_client.list_objects_v2.return_value = {}

        result = service._search_paths(Path('users/test_user/v1_conversations/abc123'))

        assert len(result) == 0

    def test_search_paths_with_page_id(self, service: AwsEventService, mock_s3_client):
        """Test that _search_paths uses continuation token."""
        mock_s3_client.list_objects_v2.return_value = {
            'Contents': [{'Key': 'event.json'}]
        }

        service._search_paths(Path('prefix'), page_id='continuation_token')

        mock_s3_client.list_objects_v2.assert_called_once_with(
            Bucket='test-bucket',
            Prefix='prefix',
            ContinuationToken='continuation_token',
        )


class TestAwsEventServiceIntegration:
    """Integration tests for AwsEventService."""

    @pytest.mark.asyncio
    async def test_get_conversation_path_with_user_id(self, service: AwsEventService):
        """Test conversation path generation with user_id."""
        conversation_id = uuid4()

        path = await service.get_conversation_path(conversation_id)

        assert 'users' in str(path)
        assert 'test_user' in str(path)
        assert 'v1_conversations' in str(path)
        assert conversation_id.hex in str(path)

    @pytest.mark.asyncio
    async def test_get_conversation_path_without_user_id(
        self, service_no_user: AwsEventService
    ):
        """Test conversation path generation without user_id."""
        conversation_id = uuid4()

        path = await service_no_user.get_conversation_path(conversation_id)

        assert 'users' in str(path)
        assert 'test_user' not in str(path)
        assert 'v1_conversations' in str(path)
        assert conversation_id.hex in str(path)


class TestAwsEventServiceInjector:
    """Test cases for AwsEventServiceInjector."""

    def test_injector_has_bucket_name(self):
        """Test that injector has bucket_name attribute."""
        injector = AwsEventServiceInjector(bucket_name='my-bucket')
        assert injector.bucket_name == 'my-bucket'

    def test_injector_has_default_prefix(self):
        """Test that injector has default prefix."""
        injector = AwsEventServiceInjector(bucket_name='my-bucket')
        assert injector.prefix == Path('users')


class TestUseRealAws:
    """Test cases for _use_real_aws function."""

    def test_defaults_to_false(self, monkeypatch):
        """Default should be False (LocalStack)."""
        monkeypatch.delenv('USE_AWS_S3', raising=False)
        assert not _use_real_aws()

    def test_true_value(self, monkeypatch):
        """'true' should enable real AWS."""
        monkeypatch.setenv('USE_AWS_S3', 'true')
        assert _use_real_aws()

    def test_1_value(self, monkeypatch):
        """'1' should enable real AWS (Helm chart compatibility)."""
        monkeypatch.setenv('USE_AWS_S3', '1')
        assert _use_real_aws()

    def test_false_value(self, monkeypatch):
        """'false' should disable real AWS."""
        monkeypatch.setenv('USE_AWS_S3', 'false')
        assert not _use_real_aws()

    def test_case_insensitive(self, monkeypatch):
        """Values should be case-insensitive."""
        monkeypatch.setenv('USE_AWS_S3', 'TRUE')
        assert _use_real_aws()


class TestEnsureUrlScheme:
    """Test cases for _ensure_url_scheme function."""

    def test_secure_adds_https_prefix(self):
        assert _ensure_url_scheme(True, 's3.amazonaws.com') == 'https://s3.amazonaws.com'

    def test_insecure_adds_http_prefix(self):
        assert _ensure_url_scheme(False, 'localhost:4566') == 'http://localhost:4566'

    def test_secure_converts_http_to_https(self):
        assert _ensure_url_scheme(True, 'http://minio.example.com:9000') == 'https://minio.example.com:9000'

    def test_insecure_converts_https_to_http(self):
        assert _ensure_url_scheme(False, 'https://minio.example.com:9000') == 'http://minio.example.com:9000'

    def test_secure_preserves_existing_https(self):
        assert _ensure_url_scheme(True, 'https://s3.amazonaws.com') == 'https://s3.amazonaws.com'

    def test_insecure_preserves_existing_http(self):
        assert _ensure_url_scheme(False, 'http://localhost:4566') == 'http://localhost:4566'

    def test_none_returns_none(self):
        assert _ensure_url_scheme(True, None) is None
        assert _ensure_url_scheme(False, None) is None


class TestCreateS3Client:
    """Test cases for create_s3_client factory function."""

    def test_localstack_creates_client_with_correct_params(self, monkeypatch):
        """LocalStack mode should pass endpoint_url and use_ssl=False."""
        monkeypatch.setenv('USE_AWS_S3', 'false')
        monkeypatch.setenv('LOCALSTACK_ENDPOINT', 'http://localstack:4566')
        monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'test-key')
        monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test-secret')

        with patch('boto3.client') as mock_client:
            create_s3_client()

        mock_client.assert_called_once()
        _, kwargs = mock_client.call_args
        assert kwargs['endpoint_url'] == 'http://localstack:4566'
        assert kwargs['use_ssl'] is False
        assert kwargs['aws_access_key_id'] == 'test-key'
        assert kwargs['aws_secret_access_key'] == 'test-secret'
        assert 'config' in kwargs

    def test_localstack_defaults_test_credentials(self, monkeypatch):
        """LocalStack should default to 'test' credentials when none set."""
        monkeypatch.setenv('USE_AWS_S3', 'false')
        monkeypatch.delenv('AWS_ACCESS_KEY_ID', raising=False)
        monkeypatch.delenv('AWS_SECRET_ACCESS_KEY', raising=False)

        with patch('boto3.client') as mock_client:
            create_s3_client()

        _, kwargs = mock_client.call_args
        assert kwargs['aws_access_key_id'] == 'test'
        assert kwargs['aws_secret_access_key'] == 'test'

    def test_real_aws_no_custom_endpoint(self, monkeypatch):
        """Real AWS mode should not pass endpoint_url."""
        monkeypatch.setenv('USE_AWS_S3', 'true')

        with patch('boto3.client') as mock_client:
            create_s3_client()

        mock_client.assert_called_once()
        _, kwargs = mock_client.call_args
        assert 'endpoint_url' not in kwargs or kwargs['endpoint_url'] is None

    def test_real_aws_uses_standard_resolution(self, monkeypatch):
        """Real AWS should call boto3.client without extra config."""
        monkeypatch.setenv('USE_AWS_S3', '1')

        with patch('boto3.client') as mock_client:
            create_s3_client()

        mock_client.assert_called_once_with('s3')
