"""Tests for AutomationEventCallbackProcessor.

The callback processor handles post-execution operations using the
deterministic service layer — git commit/push, PR creation, Jira
transitions, and token usage reporting.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from openhands.app_server.automation.callback_processors import (
    AutomationEventCallbackProcessor,
)
from openhands.sdk.event.conversation_state import ConversationStateUpdateEvent


@pytest.fixture
def processor():
    return AutomationEventCallbackProcessor()


@pytest.fixture
def conversation_id():
    return uuid4()


@pytest.fixture
def callback():
    cb = MagicMock()
    cb.id = uuid4()
    cb.status = 'active'
    return cb


@pytest.fixture
def mock_execution_record():
    """Create a mock execution record (Jira source)."""
    rec = MagicMock()
    rec.execution_id = 'exec_test123'
    rec.source_type = 'jira'
    rec.jira_issue_key = 'KAN-123'
    rec.github_pr_id = None
    rec.repository = 'owner/repo'
    rec.branch = 'feature/KAN-123-fix'
    return rec


def _make_event(key: str, value: str):
    """Helper to create a ConversationStateUpdateEvent."""
    event = MagicMock(spec=ConversationStateUpdateEvent)
    event.key = key
    event.value = value
    event.id = str(uuid4())
    return event


@pytest.mark.asyncio
async def test_non_state_update_event(processor, conversation_id, callback):
    """Non-ConversationStateUpdateEvent returns None."""
    event = MagicMock()
    event.key = 'execution_status'
    event.value = 'finished'

    result = await processor(conversation_id, callback, event)
    assert result is None


@pytest.mark.asyncio
async def test_non_execution_status_key(processor, conversation_id, callback):
    """Events with non-execution_status key return None."""
    event = _make_event('some_other_key', 'finished')

    result = await processor(conversation_id, callback, event)
    assert result is None


@pytest.mark.asyncio
async def test_non_terminal_status(processor, conversation_id, callback):
    """Non-terminal status events return None."""
    event = _make_event('execution_status', 'running')

    result = await processor(conversation_id, callback, event)
    assert result is None


@pytest.mark.asyncio
async def test_no_execution_record_returns_none(
    processor, conversation_id, callback,
):
    """When no execution record is found, returns None."""
    event = _make_event('execution_status', 'finished')

    with patch(
        'openhands.app_server.automation.callback_processors.ExecutionStore',
    ) as MockStore:
        store_instance = MockStore.return_value
        store_instance.get_execution_by_conversation_id = AsyncMock(
            return_value=None
        )

        result = await processor(conversation_id, callback, event)

    assert result is None


@pytest.mark.asyncio
async def test_terminal_finished_updates_state(
    processor, conversation_id, callback, mock_execution_record,
):
    """Terminal FINISHED updates execution to COMPLETED."""
    event = _make_event('execution_status', 'finished')

    with (
        patch(
            'openhands.app_server.automation.callback_processors.ExecutionStore',
        ) as MockStore,
        patch.object(processor, '_run_post_execution') as mock_post,
        patch.object(processor, '_pause_sandbox') as mock_pause,
    ):
        store_instance = MockStore.return_value
        store_instance.get_execution_by_conversation_id = AsyncMock(
            return_value=mock_execution_record
        )
        store_instance.update_state = AsyncMock()

        result = await processor(conversation_id, callback, event)

    assert result is not None
    assert result.status.value == 'SUCCESS'
    assert callback.status.value == 'COMPLETED'

    # Post-execution should NOT be called without request context
    mock_post.assert_not_called()
    mock_pause.assert_not_called()

    # State should be updated to COMPLETED
    store_instance.update_state.assert_awaited_once_with(
        execution_id='exec_test123',
        state='COMPLETED',
        conversation_id=str(conversation_id),
    )


@pytest.mark.asyncio
async def test_terminal_error_updates_state(
    processor, conversation_id, callback, mock_execution_record,
):
    """Terminal ERROR updates execution to FAILED."""
    event = _make_event('execution_status', 'error')

    with patch(
        'openhands.app_server.automation.callback_processors.ExecutionStore',
    ) as MockStore:
        store_instance = MockStore.return_value
        store_instance.get_execution_by_conversation_id = AsyncMock(
            return_value=mock_execution_record
        )
        store_instance.update_state = AsyncMock()

        result = await processor(conversation_id, callback, event)

    assert result is not None
    assert result.status.value == 'SUCCESS'

    # Verify FAILED state
    call_kwargs = store_instance.update_state.call_args.kwargs
    assert call_kwargs['state'].value == 'FAILED'


@pytest.mark.asyncio
async def test_terminal_finished_with_context_runs_post_execution(
    processor, conversation_id, callback, mock_execution_record,
):
    """With request context, FINISHED triggers post-execution."""
    event = _make_event('execution_status', 'finished')

    # Inject request context
    processor._state = MagicMock()
    processor._request = MagicMock()

    with (
        patch(
            'openhands.app_server.automation.callback_processors.ExecutionStore',
        ) as MockStore,
        patch.object(processor, '_run_post_execution') as mock_post,
        patch.object(processor, '_pause_sandbox') as mock_pause,
    ):
        store_instance = MockStore.return_value
        store_instance.get_execution_by_conversation_id = AsyncMock(
            return_value=mock_execution_record
        )
        store_instance.update_state = AsyncMock()

        result = await processor(conversation_id, callback, event)

    assert result is not None
    assert result.status.value == 'SUCCESS'

    # Post-execution should be called
    mock_post.assert_awaited_once_with(
        mock_execution_record, conversation_id,
    )
    mock_pause.assert_awaited_once_with(conversation_id)


@pytest.mark.asyncio
async def test_post_execution_jira_flow(
    processor, conversation_id, mock_execution_record,
):
    """Jira post-execution commits, pushes, creates PR, transitions, posts tokens."""
    cid_str = str(conversation_id)

    with (
        patch.object(processor, '_resolve_sandbox_info') as mock_resolve,
        patch(
            'openhands.app_server.automation.services.sandbox_git_service.SandboxGitService',
        ) as MockGit,
        patch(
            'openhands.app_server.automation.services.github_api_service.GitHubApiService',
        ) as MockGh,
        patch(
            'openhands.app_server.automation.services.jira_api_service.JiraApiService',
        ) as MockJira,
        patch(
            'openhands.app_server.automation.services.metrics_service.MetricsService',
        ) as MockMetrics,
    ):
        mock_resolve.return_value = ('http://agent:8000', 'sk_test')

        git_instance = MockGit.return_value
        git_instance.has_changes = AsyncMock(return_value=True)
        git_instance.commit_all = AsyncMock(return_value='abc1234')
        git_instance.push = AsyncMock()

        gh_instance = MockGh.return_value
        gh_instance.create_pull_request = AsyncMock(
            return_value={'number': 42, 'html_url': 'https://github.com/owner/repo/pull/42'}
        )

        jira_instance = MockJira.return_value
        jira_instance.transition_issue = MagicMock()
        jira_instance.add_or_update_token_usage_comment = MagicMock()

        metrics_instance = MockMetrics.return_value
        metrics_instance.fetch_live_metrics = AsyncMock(
            return_value={
                'accumulated_cost': 0.05,
                'model_name': 'gpt-4',
                'prompt_tokens': 600,
                'completion_tokens': 200,
                'cache_read_tokens': 0,
                'cache_write_tokens': 0,
                'reasoning_tokens': 0,
                'created_at': '2025-01-01T00:00:00Z',
                'updated_at': '2025-01-01T00:01:00Z',
            },
        )
        metrics_instance.build_token_usage_comment = MagicMock(
            return_value={'type': 'doc', 'content': []},
        )

        await processor._handle_jira_post(
            mock_execution_record, 'http://agent:8000', 'sk_test', cid_str,
        )

    # Git operations
    git_instance.has_changes.assert_awaited_once()
    git_instance.commit_all.assert_awaited_once_with(
        '[Automation] KAN-123: code changes from OpenHands',
    )
    git_instance.push.assert_awaited_once_with('feature/KAN-123-fix')

    # PR creation
    gh_instance.create_pull_request.assert_awaited_once()

    # Jira transition
    jira_instance.transition_issue.assert_called_once_with(
        'KAN-123', 'In Review',
    )

    # Metrics
    metrics_instance.fetch_live_metrics.assert_awaited_once_with(
        'http://agent:8000', cid_str, 'sk_test',
    )

    # Token comment
    jira_instance.add_or_update_token_usage_comment.assert_called_once()


@pytest.mark.asyncio
async def test_post_execution_jira_no_changes(
    processor, conversation_id, mock_execution_record,
):
    """Jira post-execution skips commits/PR when no changes."""
    cid_str = str(conversation_id)

    with (
        patch.object(processor, '_resolve_sandbox_info') as mock_resolve,
        patch(
            'openhands.app_server.automation.services.sandbox_git_service.SandboxGitService',
        ) as MockGit,
        patch(
            'openhands.app_server.automation.services.jira_api_service.JiraApiService',
        ) as MockJira,
        patch(
            'openhands.app_server.automation.services.metrics_service.MetricsService',
        ) as MockMetrics,
    ):
        mock_resolve.return_value = ('http://agent:8000', 'sk_test')

        git_instance = MockGit.return_value
        git_instance.has_changes = AsyncMock(return_value=False)

        jira_instance = MockJira.return_value
        jira_instance.transition_issue = MagicMock()

        metrics_instance = MockMetrics.return_value
        metrics_instance.fetch_live_metrics = AsyncMock(return_value={})

        await processor._handle_jira_post(
            mock_execution_record, 'http://agent:8000', 'sk_test', cid_str,
        )

    # No commit if no changes
    git_instance.commit_all.assert_not_called()
    git_instance.push.assert_not_called()

    # Jira transition should still happen
    jira_instance.transition_issue.assert_called_once_with(
        'KAN-123', 'In Review',
    )

    # No token comment if metrics empty
    metrics_instance.build_token_usage_comment.assert_not_called()
    jira_instance.add_or_update_token_usage_comment.assert_not_called()


@pytest.mark.asyncio
async def test_post_execution_github_flow(
    processor, conversation_id,
):
    """GitHub post-execution commits, pushes, and comments on PR."""
    record = MagicMock()
    record.source_type = 'github'
    record.repository = 'owner/repo'
    record.github_pr_id = 100
    record.branch = 'fix/test-branch'
    record.jira_issue_key = None

    with (
        patch.object(processor, '_resolve_sandbox_info') as mock_resolve,
        patch(
            'openhands.app_server.automation.services.sandbox_git_service.SandboxGitService',
        ) as MockGit,
        patch(
            'openhands.app_server.automation.services.github_api_service.GitHubApiService',
        ) as MockGh,
    ):
        mock_resolve.return_value = ('http://agent:8000', 'sk_test')

        git_instance = MockGit.return_value
        git_instance.has_changes = AsyncMock(return_value=True)
        git_instance.commit_all = AsyncMock(return_value='def5678')
        git_instance.push = AsyncMock()

        gh_instance = MockGh.return_value
        gh_instance.add_pr_comment = AsyncMock()

        await processor._handle_github_post(
            record, 'http://agent:8000', 'sk_test',
        )

    git_instance.commit_all.assert_awaited_once_with(
        '[Automation] PR #100: code review changes',
    )
    git_instance.push.assert_awaited_once_with('fix/test-branch')
    gh_instance.add_pr_comment.assert_awaited_once()

    # Verify comment mentions review complete
    # add_pr_comment is called with (repo, pr_number, body)
    comment_body = gh_instance.add_pr_comment.call_args[0][2]
    assert 'review complete' in comment_body
    assert 'fix/test-branch' in comment_body
