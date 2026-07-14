"""Archive and restore sandbox state to/from S3 for automation flows.

Replaces the old pause-on-completion pattern: when an agent finishes a
task the conversation events + workspace diff are uploaded to S3 and the
sandbox is destroyed.  When a follow-up event arrives later a fresh
sandbox is created, the archive is downloaded and restored, and the
conversation continues with full context via the SDK's built-in resume
path.
"""

from __future__ import annotations

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
                f'{agent_server_url}/file/download-trajectory/{conversation_id}'
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

            # 3. Upload to S3
            s3_key = f'archives/{mapping_key}/{execution_id}.tar.gz'
            self._s3.write(s3_key, archive_data)
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
        try:
            archive_data = self._s3.read(s3_key)
            if isinstance(archive_data, str):
                archive_data = archive_data.encode('utf-8')
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
                    f'{agent_server_url}/file/upload?path=/tmp/_restore.zip',
                    headers=headers,
                    files={'file': conv_zip.read_bytes()},
                    timeout=120.0,
                )
                upload_resp.raise_for_status()

                # Extract into conversations dir
                dest = f'/home/openhands/{conversations_path}/{conversation_id}'
                bash_resp = await self._httpx.post(
                    f'{agent_server_url}/api/bash',
                    json={
                        'command': (
                            f'mkdir -p {dest} && '
                            f'unzip -o /tmp/_restore.zip -d {dest} && '
                            f'rm -f {dest}/owner_lease.json {dest}/lease.lock '
                            f'{dest}/.eventlog.lock && '
                            f'rm /tmp/_restore.zip'
                        ),
                        'run': True,
                    },
                    headers=headers,
                    timeout=30.0,
                )
                bash_resp.raise_for_status()

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
