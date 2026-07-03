"""Security hook files injected into target repos at setup time.

These files define the PreToolUse hook that blocks dangerous commands
(git branch -D main, git push --force main, etc.) before execution.
They are shipped into the sandbox via base64-encoded execute_command
calls so they exist even when the target repo has no .openhands/ dir.

Files are read from disk at import time, then shipped as base64 text
via workspace.execute_command during run_setup_scripts.
"""

import base64
import logging
import pathlib

_logger = logging.getLogger(__name__)

# Path to the OpenHands server repo's .openhands/ directory
# This is resolved relative to THIS file's location in the source tree.
_HERE = pathlib.Path(__file__).resolve().parent.parent.parent.parent
_OPENHANDS_DIR = _HERE / '.openhands'


def _load_file(name: str) -> str | None:
    """Load a file from the OpenHands server repo's .openhands/ directory."""
    path = _OPENHANDS_DIR / name
    if not path.exists():
        _logger.warning('Hook file not found: %s', path)
        return None
    return path.read_text()


# Load hooks.json — the PreToolUse hook configuration
HOOKS_JSON_STR: str | None = _load_file('hooks.json')

# Load block_dangerous.sh — the actual shell script that checks patterns
BLOCK_DANGEROUS_STR: str | None = _load_file('hooks/block_dangerous.sh')


async def inject_hooks_into_sandbox(
    execute_command,
    project_dir: str,
) -> None:
    """Write hook files into the target repo's .openhands/ directory.

    Runs inside the sandbox via workspace.execute_command.

    Args:
        execute_command: An async callable (e.g. workspace.execute_command)
            that runs a command in the sandbox.
        project_dir: The target repo's root directory inside the sandbox.
    """
    if not HOOKS_JSON_STR or not BLOCK_DANGEROUS_STR:
        _logger.warning(
            'Cannot inject security hooks: hook files not found on disk'
        )
        return

    hooks_json_b64 = base64.b64encode(HOOKS_JSON_STR.encode()).decode()
    script_b64 = base64.b64encode(BLOCK_DANGEROUS_STR.encode()).decode()

    cmd = (
        f'mkdir -p "{project_dir}/.openhands/hooks" && '
        f'echo "{hooks_json_b64}" | base64 -d > "{project_dir}/.openhands/hooks.json" && '
        f'echo "{script_b64}" | base64 -d > "{project_dir}/.openhands/hooks/block_dangerous.sh" && '
        f'chmod +x "{project_dir}/.openhands/hooks/block_dangerous.sh"'
    )

    await execute_command(cmd, timeout=30)
