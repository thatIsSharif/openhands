"""Archive and restore sandbox state to/from S3 for automation flows.

Replaces the old pause-on-completion pattern: when an agent finishes a
task the conversation events + workspace diff are uploaded to S3 and the
sandbox is destroyed.  When a follow-up event arrives later a fresh
sandbox is created, the archive is downloaded and restored, and the
conversation continues with full context via the SDK's built-in resume
path.
"""

from __future__ import annotations

import base64
import io
import os
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from openhands.app_server.file_store.s3 import S3FileStore
from openhands.app_server.utils.logger import openhands_logger as logger

if TYPE_CHECKING:
    pass


class SandboxArchiveService:
    """Archives sandbox state to S3 and restores it into a fresh sandbox.

    S3 key schema::

        archives/jira/{issue_key}/{execution_id}.tar.gz
        archives/github/{owner}/{repo}/pr-{number}/{execution_id}.tar.gz
    """

    def __init__(
        self,
        s3_store: S3FileStore,
        httpx_client: httpx.AsyncClient,
    ) -> None:
        self._s3 = s3_store
        self._httpx = httpx_client

    # ── archive ───────────────────────────────────────────────────────

    async def archive_and_cleanup(
        self,
        *,
        agent_server_url: str,
        session_api_key: str,
        sandbox_id: str,
        conversation_id: str,
        execution_id: str,
        mapping_key: str,
        sandbox_service,
    ) -> str | None:
        """Pack conversation + workspace, upload to S3, destroy sandbox.

        Returns the S3 key on success, ``None`` on failure.
        """
        headers = {'X-Session-API-Key': session_api_key}

        try:
            # 1. Download conversation trajectory as zip
            traj_url = (
                f'{agent_server_url}/api/file/download-trajectory/{conversation_id}'
            )
            resp = await self._httpx.get(
                traj_url, headers=headers, timeout=120.0
            )
            resp.raise_for_status()
        except Exception:
            logger.error(
                '[SandboxArchive] Failed to download trajectory for '
                'conversation %s', conversation_id, exc_info=True,
            )
            return None

        try:
            # 2. Build archive tarball in memory
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as tar:
                # Add conversation archive (the zip from /download-trajectory)
                conv_info = tarfile.TarInfo(name='conversation.zip')
                conv_data = resp.content
                conv_info.size = len(conv_data)
                tar.addfile(conv_info, io.BytesIO(conv_data))

                # Add metadata about execution / mapping
                meta = (
                    f'execution_id={execution_id}\n'
                    f'conversation_id={conversation_id}\n'
                    f'mapping_key={mapping_key}\n'
                )
                meta_bytes = meta.encode('utf-8')
                meta_info = tarfile.TarInfo(name='archive-meta.txt')
                meta_info.size = len(meta_bytes)
                tar.addfile(meta_info, io.BytesIO(meta_bytes))

            archive_data = buf.getvalue()

            # 3. Upload to S3 (base64-encode: S3FileStore.read returns str)
            s3_key = f'archives/{mapping_key}/{execution_id}.tar.gz'
            self._s3.write(s3_key, base64.b64encode(archive_data).decode('ascii'))
            logger.info(
                '[SandboxArchive] Uploaded %d bytes to s3://%s/%s',
                len(archive_data), self._s3._get_bucket_name(), s3_key,
            )

            # 4. Destroy sandbox
            await sandbox_service.delete_sandbox(sandbox_id)
            logger.info(
                '[SandboxArchive] Sandbox %s destroyed after archive', sandbox_id,
            )

            return s3_key

        except Exception:
            logger.error(
                '[SandboxArchive] Failed to archive sandbox %s',
                sandbox_id, exc_info=True,
            )
            return None

    # ── restore ───────────────────────────────────────────────────────

    async def restore_into_sandbox(
        self,
        *,
        agent_server_url: str,
        session_api_key: str,
        s3_key: str,
        conversation_id: str,
        conversations_path: str = 'workspace/conversations',
    ) -> bool:
        """Download archive from S3 and populate a fresh sandbox.

        After this call the conversation directory contains
        ``base_state.json``, ``meta.json``, and all event files — the
        next ``POST /api/conversations`` with the same ``conversation_id``
        will automatically take the resume path.
        """
        import uuid as _uuid

        # The agent-server persistence layer stores conversations under
        # ``conversations_dir / conversation_id.hex`` (UUID without dashes).
        # The download-trajectory zip contains only the file contents (no
        # outer directory), so we must restore into the hex‑named directory.
        # conversation_id may be a UUID object (from Pydantic model) or a
        # string (from execution store). Normalize before calling `.hex`.
        if isinstance(conversation_id, _uuid.UUID):
            cid_hex = conversation_id.hex
        else:
            try:
                cid_hex = _uuid.UUID(conversation_id).hex
            except (ValueError, AttributeError, TypeError):
                logger.error(
                    '[SandboxArchive] Invalid conversation_id %r',
                    conversation_id,
                    exc_info=True,
                )
                return False

        try:
            archive_b64 = self._s3.read(s3_key)
            archive_data = base64.b64decode(archive_b64)
        except Exception:
            logger.error(
                '[SandboxArchive] Failed to read s3://%s/%s',
                self._s3._get_bucket_name(), s3_key, exc_info=True,
            )
            return False

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)

                # Extract archive to temp
                with tarfile.open(
                    fileobj=io.BytesIO(archive_data), mode='r:gz'
                ) as tar:
                    tar.extractall(tmp_path)

                conv_zip = tmp_path / 'conversation.zip'
                if not conv_zip.exists():
                    logger.error(
                        '[SandboxArchive] Archive missing conversation.zip'
                    )
                    return False

                # Upload the zip to the sandbox, then extract via bash
                headers = {'X-Session-API-Key': session_api_key}

                # Upload the archive to a temp location inside the sandbox
                upload_resp = await self._httpx.post(
                    f'{agent_server_url}/api/file/upload?path=/tmp/_restore.zip',
                    headers=headers,
                    files={'file': conv_zip.read_bytes()},
                    timeout=120.0,
                )
                upload_resp.raise_for_status()

                # Determine the agent server process CWD by reading
                # /proc/<pid>/cwd.  ``pwd`` from the bash API gives the
                # bash-session CWD, NOT the agent-server CWD, and the
                # config.conversations_path is resolved relative to the
                # *agent-server* CWD (``Path('workspace/conversations')``
                # becomes ``/workspace/conversations`` when the server runs
                # from ``/``).
                cwd_cmd = (
                    'python3 -c "'
                    'import os;'
                    "for e in os.listdir('/proc'):"
                    '  if e.isdigit():'
                    '    try:'
                    "      cl = open(f'/proc/{e}/cmdline').read()"
                    "      if 'python' in cl and ('agent_server' in cl"
                    "                         or 'uvicorn' in cl):"
                    "        print(os.readlink(f'/proc/{e}/cwd')); break"
                    '    except: pass'
                    '"'
                )
                cwd_resp = await self._httpx.post(
                    f'{agent_server_url}/api/bash/execute_bash_command',
                    json={'command': cwd_cmd},
                    headers=headers,
                    timeout=10.0,
                )
                cwd_resp.raise_for_status()
                cwd_output = cwd_resp.json().get('stdout', '') or ''
                agent_cwd = cwd_output.strip()
                if not agent_cwd:
                    logger.warning(
                        '[SandboxArchive] Could not detect agent-server CWD, '
                        'falling back to /'
                    )
                    agent_cwd = '/'

                # Extract into conversations dir
                dest = f'{agent_cwd}/{conversations_path}/{cid_hex}'
                # Use python3 zipfile (unzip is not in the sandbox image)
                extract_cmd = (
                    'python3 -c "'
                    f"import zipfile, pathlib;"
                    f"d=pathlib.Path('{dest}');"
                    f"d.mkdir(parents=True, exist_ok=True);"
                    f"zipfile.ZipFile('/tmp/_restore.zip').extractall(d);"
                    f"for f in ['owner_lease.json','lease.lock','.eventlog.lock']:"
                    f" (d/f).unlink(missing_ok=True);"
                    f"pathlib.Path('/tmp/_restore.zip').unlink(missing_ok=True)\""
                )
                bash_resp = await self._httpx.post(
                    f'{agent_server_url}/api/bash/execute_bash_command',
                    json={'command': extract_cmd},
                    headers=headers,
                    timeout=30.0,
                )
                bash_resp.raise_for_status()

                # Verify the extract landed where expected and strip stale
                # MCP config from base_state.json so the agent-server
                # doesn't try to reconnect to dead MCP servers from the
                # old sandbox container.
                verify_cmd = (
                    'python3 -c "'
                    'import json, pathlib, sys;'
                    f"d=pathlib.Path('{dest}');"
                    'bs=d/\'base_state.json\';'
                    'if not bs.exists():'
                    "  print(f'base_state.json NOT FOUND at {bs}');"
                    '  sys.exit(1);'
                    'data=json.loads(bs.read_text());'
                    "data.pop('mcp_config',None);"
                    "ag=data.get('agent',{});"
                    "ag.pop('mcp_config',None);"
                    "data['agent']=ag;"
                    'bs.write_text(json.dumps(data,indent=2));'
                    "print(f'OK base_state.json at {bs}');"
                    'print(f\'events_count={len(list(d.glob(\"events/*.json\")))}\')"'
                )
                verify_resp = await self._httpx.post(
                    f'{agent_server_url}/api/bash/execute_bash_command',
                    json={'command': verify_cmd},
                    headers=headers,
                    timeout=15.0,
                )
                verify_resp.raise_for_status()
                verify_out = verify_resp.json().get('stdout', '') or ''
                logger.info(
                    '[SandboxArchive] Restore verify: %s', verify_out.strip()
                )

                logger.info(
                    '[SandboxArchive] Restored conversation %s from %s',
                    conversation_id, s3_key,
                )
                return True

        except Exception:
            logger.error(
                '[SandboxArchive] Failed to restore %s for conversation %s',
                s3_key, conversation_id, exc_info=True,
            )
            return False

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    async def clone_repo(
        *,
        httpx_client,
        agent_server_url: str,
        session_api_key: str,
        repo_owner: str,
        repo_name: str,
        branch: str = 'main',
        target_dir: str = '/workspace/project',
        git_provider: str = 'github.com',
    ) -> bool:
        """Clone a git repository into the sandbox workspace.

        Uses the agent server's ``/api/bash`` endpoint so the clone
        happens directly inside the sandbox.  Requires ``GITHUB_PAT_TOKEN``
        (or equivalent) to be configured in the sandbox environment.
        """
        clone_url = f'https://{git_provider}/{repo_owner}/{repo_name}.git'
        # Never delete target_dir itself — the agent-server Docker image
        # uses WORKDIR /workspace/project as its CWD.  If we rm -rf that
        # directory, the kernel orphans the process's CWD dentry and
        # os.getcwd() raises FileNotFoundError, which crashes
        # Laminar/OpenTelemetry instrumentation (find_dotenv) and causes
        # litellm.APIConnectionError: [Errno 2] No such file or directory.
        #
        # Strategy: try fresh clone first.  If the directory already
        # exists (second invocation), cd into it and update in-place via
        # fetch + checkout + reset.  Never rm -rf the CWD.
        cmd = (
            f'git clone -b {branch} --single-branch '
            f'{clone_url} {target_dir} 2>/dev/null || '
            f'(cd {target_dir} && '
            f'git fetch origin && '
            f'git checkout -f {branch} && '
            f'git reset --hard origin/{branch} && '
            f'git clean -fd)'
        )
        headers = {'X-Session-API-Key': session_api_key}
        try:
            resp = await httpx_client.post(
                f'{agent_server_url}/api/bash/execute_bash_command',
                json={'command': cmd},
                headers=headers,
                timeout=300.0,
            )
            resp.raise_for_status()
            logger.info(
                '[SandboxArchive] Cloned %s/%s (branch=%s) → %s',
                repo_owner, repo_name, branch, target_dir,
            )
            return True
        except Exception:
            logger.error(
                '[SandboxArchive] Failed to clone %s/%s',
                repo_owner, repo_name, exc_info=True,
            )
            return False

    @staticmethod
    def build_mapping_key(
        *,
        jira_issue_key: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pr_number: int | None = None,
    ) -> str:
        """Build the S3 prefix key from source identifiers."""
        if jira_issue_key:
            return f'jira/{jira_issue_key}'
        if owner and repo and pr_number is not None:
            return f'github/{owner}/{repo}/pr-{pr_number}'
        raise ValueError('Cannot build mapping key: no source identifiers')
