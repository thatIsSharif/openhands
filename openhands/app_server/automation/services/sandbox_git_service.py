"""Sandbox Git operations service.

Runs git commands inside the sandbox via AsyncRemoteWorkspace.
Handles all deterministic git operations that were previously
handled by the LLM.
"""

from __future__ import annotations

from openhands.sdk.workspace.remote.async_remote_workspace import (
    AsyncRemoteWorkspace,
)


class SandboxGitService:
    """Runs git operations inside the sandbox via the agent server's API.

    All methods raise RuntimeError on non-zero exit from git commands.
    """

    def __init__(
        self,
        agent_server_url: str,
        session_api_key: str,
        project_dir: str,
    ) -> None:
        self._workspace = AsyncRemoteWorkspace(
            host=agent_server_url,
            api_key=session_api_key,
            working_dir=project_dir,
        )
        self._project_dir = project_dir

    async def _run_git(self, *args: str) -> str:
        """Run a git command in the sandbox and return stdout.

        Raises:
            RuntimeError: If the git command exits with non-zero status.
        """
        cmd = f'git {" ".join(args)}'
        result = await self._workspace.execute_command(
            command=cmd,
            cwd=self._project_dir,
            timeout=30.0,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f'Git command failed (exit={result.exit_code}): '
                f'{cmd}\n{result.stderr or result.stdout}'
            )
        return result.stdout

    async def get_diff_stat(self) -> str:
        """Return output of 'git diff --stat' (empty string if no changes)."""
        return await self._run_git('diff', '--stat')

    async def get_diff(self) -> str:
        """Return full output of 'git diff' (empty if no changes)."""
        return await self._run_git('diff')

    async def has_changes(self) -> bool:
        """Check if there are any uncommitted changes in the working tree."""
        stat = await self.get_diff_stat()
        return bool(stat.strip())

    async def create_branch(self, branch: str, base: str) -> None:
        """Create and checkout a new branch from the given base."""
        await self._run_git('checkout', '-b', branch, base)

    async def checkout(self, branch: str) -> None:
        """Checkout an existing branch."""
        await self._run_git('checkout', branch)

    async def commit_all(self, message: str) -> str:
        """Stage all changes and commit.

        Returns the commit hash.
        """
        await self._run_git('add', '-A')
        output = await self._run_git('commit', '-m', message)
        # Extract commit hash from output like "[branch abc1234] message"
        for line in output.splitlines():
            if 'commit' in line:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == 'commit' and i + 1 < len(parts):
                        return parts[i + 1].rstrip('.')
        return output.strip()

    async def push(self, branch: str) -> str:
        """Push the given branch to origin.

        Returns the push output.
        """
        return await self._run_git('push', 'origin', branch)
