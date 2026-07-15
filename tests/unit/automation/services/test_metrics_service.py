"""Tests for MetricsService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands.app_server.automation.services.metrics_service import (
    MetricsService,
)


@pytest.fixture
def metrics_service():
    return MetricsService()


@pytest.mark.asyncio
async def test_fetch_live_metrics_success(metrics_service):
    """fetch_live_metrics returns formatted metrics on success."""
    api_response = {
        'stats': {
            'usage_to_metrics': {
                'agent': {
                    'accumulated_cost': 0.005,
                    'model_name': 'gpt-4',
                    'accumulated_token_usage': {
                        'prompt_tokens': 500,
                        'completion_tokens': 200,
                        'cache_read_tokens': 100,
                        'cache_write_tokens': 50,
                        'reasoning_tokens': 10,
                    },
                }
            }
        },
        'created_at': '2024-01-01T00:00:00Z',
        'updated_at': '2024-01-01T01:00:00Z',
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = api_response

    with patch('httpx.AsyncClient') as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_resp
        )
        result = await metrics_service.fetch_live_metrics(
            agent_server_url='http://localhost:60000',
            conversation_id='conv-123',
            session_api_key='test-key',
        )

    assert result['accumulated_cost'] == 0.005
    assert result['model_name'] == 'gpt-4'
    assert result['prompt_tokens'] == 500
    assert result['completion_tokens'] == 200
    assert result['cache_read_tokens'] == 100
    assert result['cache_write_tokens'] == 50
    assert result['reasoning_tokens'] == 10


@pytest.mark.asyncio
async def test_fetch_live_metrics_failure(metrics_service):
    """fetch_live_metrics returns empty dict on failure."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500

    with patch('httpx.AsyncClient') as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=mock_resp
        )
        result = await metrics_service.fetch_live_metrics(
            agent_server_url='http://localhost:60000',
            conversation_id='conv-123',
            session_api_key='test-key',
        )

    assert result == {}


def test_build_token_usage_comment(metrics_service):
    """build_token_usage_comment returns valid ADF document."""
    result = metrics_service.build_token_usage_comment(
        accumulated_cost=0.005,
        model_name='gpt-4',
        prompt_tokens=500,
        completion_tokens=200,
    )

    assert result['type'] == 'doc'
    assert result['version'] == 1
    assert len(result['content']) > 0


def test_build_token_usage_comment_with_marker(metrics_service):
    """The token usage comment contains the marker text."""
    result = metrics_service.build_token_usage_comment(
        accumulated_cost=0.0,
        model_name='default',
        prompt_tokens=0,
        completion_tokens=0,
    )

    # Flatten content to check for marker
    raw = str(result)
    assert metrics_service.TOKEN_USAGE_MARKER in raw


def test_build_token_usage_comment_budget(metrics_service):
    """Budget bar is included when max_budget is provided."""
    result = metrics_service.build_token_usage_comment(
        accumulated_cost=0.05,
        model_name='gpt-4',
        prompt_tokens=100,
        completion_tokens=50,
        max_budget=1.0,
    )

    raw = str(result)
    assert 'Budget' in raw


def test_build_token_usage_comment_empty(metrics_service):
    """Empty metrics produce a valid ADF document."""
    result = metrics_service.build_token_usage_comment()

    assert result['type'] == 'doc'
    assert result['version'] == 1
