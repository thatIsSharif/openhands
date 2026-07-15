"""Tests for SandboxGitService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands.app_server.automation.services.sandbox_git_service import (
    SandboxGitService,
)


@pytest.fixture
def git_service():
    with patch(
        'openhands.app_server.automation.services.sandbox_git_service.AsyncRemoteWorkspace',
    ) as MockWorkspace:
        mock_ws = MockWorkspace.return_value
        service = SandboxGitService(
            agent_server_url='http://localhost:60000',
            session_api_key='test-key',
            project_dir='/workspace/project',
        )
        service._workspace = mock_ws
        yield service


def _mock_result(exit_code: int, stdout: str = '', stderr: str = ''):
    mock = MagicMock()
    mock.exit_code = exit_code
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


@pytest.mark.asyncio
async def test_get_diff_stat_no_changes(git_service):
    """get_diff_stat returns empty string when no changes."""
    git_service._workspace.execute_command = AsyncMock(
        return_value=_mock_result(0, '')
    )

    result = await git_service.get_diff_stat()
    assert result == ''


@pytest.mark.asyncio
async def test_get_diff_stat_with_changes(git_service):
    """get_diff_stat returns diff stat when changes exist."""
    git_service._workspace.execute_command = AsyncMock(
        return_value=_mock_result(0, ' file1.py | 2 +- \n 1 file changed')
    )

    result = await git_service.get_diff_stat()
    assert 'file1.py' in result
    assert '1 file changed' in result


@pytest.mark.asyncio
async def test_get_diff(git_service):
    """get_diff returns the full diff."""
    git_service._workspace.execute_command = AsyncMock(
        return_value=_mock_result(0, 'diff --git a/file.py b/file.py\n+new line')
    )

    result = await git_service.get_diff()
    assert 'diff --git' in result
    assert '+new line' in result


@pytest.mark.asyncio
async def test_has_changes_true(git_service):
    """has_changes returns True when diff stat is non-empty."""
    git_service._workspace.execute_command = AsyncMock(
        return_value=_mock_result(0, ' file1.py | 1 + \n 1 file changed')
    )

    assert await git_service.has_changes() is True


@pytest.mark.asyncio
async def test_has_changes_false(git_service):
    """has_changes returns False when diff stat is empty."""
    git_service._workspace.execute_command = AsyncMock(
        return_value=_mock_result(0, '')
    )

    assert await git_service.has_changes() is False


@pytest.mark.asyncio
async def test_create_branch(git_service):
    """create_branch runs git checkout -b with correct args."""
    git_service._workspace.execute_command = AsyncMock(
        return_value=_mock_result(0, 'Switched to a new branch')
    )

    await git_service.create_branch('feature/test', 'main')

    git_service._workspace.execute_command.assert_called_once()
    args = git_service._workspace.execute_command.call_args[1]
    assert 'checkout -b feature/test main' in args['command']
    assert args['cwd'] == '/workspace/project'


@pytest.mark.asyncio
async def test_commit_all(git_service):
    """commit_all stages and commits with the given message."""
    git_service._workspace.execute_command = AsyncMock(
        side_effect=[
            _mock_result(0),
            _mock_result(0, '[main abc1234] Test commit\n 1 file changed'),
        ]
    )

    result = await git_service.commit_all('Test commit')
    assert result.strip()


@pytest.mark.asyncio
async def test_push(git_service):
    """push runs git push origin with the branch name."""
    git_service._workspace.execute_command = AsyncMock(
        return_value=_mock_result(0, 'Everything up-to-date')
    )

    await git_service.push('feature/test')

    git_service._workspace.execute_command.assert_called_once()
    args = git_service._workspace.execute_command.call_args[1]
    assert 'push origin feature/test' in args['command']


@pytest.mark.asyncio
async def test_run_git_failure_raises(git_service):
    """Non-zero exit code raises RuntimeError."""
    git_service._workspace.execute_command = AsyncMock(
        return_value=_mock_result(1, '', 'fatal: not a git repository')
    )

    with pytest.raises(RuntimeError, match='Git command failed'):
        await git_service.get_diff_stat()
