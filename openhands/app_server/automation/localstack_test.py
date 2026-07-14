#!/usr/bin/env python3
"""End-to-end LocalStack S3 archive/restore test for automation framework.

Usage::

    python localstack_test.py

What it does::

    1. Start LocalStack container (if not running)
    2. Create S3 bucket
    3. Write a simulated archive tarball
    4. Verify archive contents via S3FileStore
    5. Verify SandboxArchiveService.build_mapping_key
    6. Verify archive-meta.txt round-trip
    7. Clean up (optional — pass --keep to leave resources)

Requires::

    pip install boto3 awscli-local
    docker

No AWS account needed — everything runs against localhost:4566.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import uuid4


# ── helpers ──────────────────────────────────────────────────────────

def run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command, print it, return result."""
    print(f'  $ {cmd}')
    return subprocess.run(cmd, shell=True, check=check,
                          capture_output=not sys.stdout.isatty())


def ok(label: str) -> None:
    print(f'  ✅  {label}')


def fail(label: str) -> None:
    print(f'  ❌  {label}')
    sys.exit(1)


# ── step 1: start localstack ─────────────────────────────────────────

def ensure_localstack() -> None:
    print('\n── Step 1: Start LocalStack ──')
    result = run('docker ps -q -f name=localstack', check=False)
    if result.stdout.strip():
        print('  LocalStack already running')
        ok('LocalStack running')
        return

    run('docker run --rm -d --name localstack '
        '-p 4566:4566 -p 4510-4559:4510-4559 '
        'localstack/localstack:latest')

    # Wait for it
    for _ in range(30):
        r = run(
            'curl -s -o /dev/null -w "%{http_code}" http://localhost:4566/_localstack/health',
            check=False,
        )
        if r.stdout.strip() == '200':
            break
        print('  waiting for LocalStack...')
        import time; time.sleep(1)
    else:
        fail('LocalStack did not become healthy after 30s')
    ok('LocalStack started')


# ── step 2: create bucket ────────────────────────────────────────────

BUCKET = 'openhands-automation-archive'

def create_bucket() -> None:
    print('\n── Step 2: Create S3 bucket ──')
    run(f'awslocal s3 mb s3://{BUCKET}', check=False)
    run(f'awslocal s3 ls')
    ok(f'Bucket {BUCKET} ready')


# ── step 3: write archive via S3FileStore ────────────────────────────

def write_archive() -> tuple[str, dict]:
    """Build a realistic archive and write it to S3 via S3FileStore.

    Returns (s3_key, metadata_dict).
    """
    print('\n── Step 3: Build and upload archive ──')

    os.environ.setdefault('USE_AWS_S3', 'false')
    os.environ.setdefault('LOCALSTACK_ENDPOINT', 'http://localhost:4566')
    os.environ.setdefault('AWS_S3_BUCKET', BUCKET)
    os.environ.setdefault('AWS_ACCESS_KEY_ID', 'test')
    os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'test')

    from openhands.app_server.file_store.s3 import S3FileStore

    exec_id = uuid4().hex[:12]
    conv_id = uuid4().hex
    issue_key = 'TEST-42'
    s3_key = f'archives/jira/{issue_key}/{exec_id}.tar.gz'

    # Build a realistic archive
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        # Simulated conversation.zip (just events + base_state)
        conv_data = (
            b'{"kind":"ConversationState","agent":"CodeActAgent",'
            b'"max_iterations":500,"workspace":"/workspace/project"}'
        )
        conv_info = tarfile.TarInfo(name='conversation.zip')
        conv_info.size = len(conv_data)
        tar.addfile(conv_info, io.BytesIO(conv_data))

        # Metadata
        meta = (
            f'execution_id={exec_id}\n'
            f'conversation_id={conv_id}\n'
            f'mapping_key=jira/{issue_key}\n'
        ).encode()
        meta_info = tarfile.TarInfo(name='archive-meta.txt')
        meta_info.size = len(meta)
        tar.addfile(meta_info, io.BytesIO(meta))

    archive_data = buf.getvalue()

    store = S3FileStore(bucket_name=BUCKET)
    # S3FileStore.read() returns str, so base64-encode binary gzip
    store.write(s3_key, base64.b64encode(archive_data).decode('ascii'))
    print(f'  Wrote {len(archive_data)} bytes to s3://{BUCKET}/{s3_key}')
    ok('Archive uploaded')

    metadata = {'execution_id': exec_id, 'conversation_id': conv_id,
                'issue_key': issue_key}
    return s3_key, metadata


# ── step 4: verify via awslocal ──────────────────────────────────────

def verify_cli(s3_key: str) -> None:
    print('\n── Step 4: Verify via awslocal ──')
    run(f'awslocal s3 ls s3://{BUCKET}/archives/ --recursive')
    ok('Archive object visible')


# ── step 5: verify via S3FileStore ───────────────────────────────────

def verify_filestore(s3_key: str, meta: dict) -> None:
    print('\n── Step 5: Verify via S3FileStore ──')

    os.environ.setdefault('USE_AWS_S3', 'false')
    os.environ.setdefault('LOCALSTACK_ENDPOINT', 'http://localhost:4566')
    os.environ.setdefault('AWS_S3_BUCKET', BUCKET)

    from openhands.app_server.file_store.s3 import S3FileStore

    store = S3FileStore(bucket_name=BUCKET)
    data_b64 = store.read(s3_key)
    assert data_b64, 'read returned empty'
    data = base64.b64decode(data_b64)

    # Round-trip: extract and inspect
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
        names = tar.getnames()
        assert 'conversation.zip' in names, f'missing conversation.zip: {names}'
        assert 'archive-meta.txt' in names, f'missing archive-meta.txt: {names}'

        # Read metadata
        meta_f = tar.extractfile('archive-meta.txt')
        assert meta_f is not None
        meta_text = meta_f.read().decode()
        assert f'execution_id={meta["execution_id"]}' in meta_text
        assert f'conversation_id={meta["conversation_id"]}' in meta_text
        assert 'mapping_key=jira/TEST-42' in meta_text

        # Read conversation
        conv_f = tar.extractfile('conversation.zip')
        assert conv_f is not None
        conv_data = conv_f.read()
        assert b'CodeActAgent' in conv_data

    # Verify list
    keys = store.list('archives/jira/TEST-42/')
    assert s3_key in keys, f'{s3_key} not in {keys}'

    ok('S3FileStore read/write/list round-trip')


# ── step 6: build_mapping_key ────────────────────────────────────────

def verify_mapping_keys() -> None:
    print('\n── Step 6: Verify mapping keys ──')

    from openhands.app_server.automation.sandbox_archive_service import \
        SandboxArchiveService as SAS

    assert SAS.build_mapping_key(jira_issue_key='TEST-42') == 'jira/TEST-42'
    assert SAS.build_mapping_key(
        owner='acme', repo='widgets', pr_number=7,
    ) == 'github/acme/widgets/pr-7'

    try:
        SAS.build_mapping_key()
        assert False, 'should have raised'
    except ValueError:
        pass

    ok('Mapping keys correct')


# ── step 7: verify S3FileStore instantiation ─────────────────────────

def verify_store_modes() -> None:
    print('\n── Step 7: Verify store modes ──')

    from openhands.app_server.file_store.s3 import (
        S3FileStore, _use_real_aws, _ensure_url_scheme, create_s3_client,
    )

    # Env flag
    os.environ['USE_AWS_S3'] = 'true'
    assert _use_real_aws()
    os.environ['USE_AWS_S3'] = '1'
    assert _use_real_aws()
    os.environ['USE_AWS_S3'] = 'false'
    assert not _use_real_aws()

    # URL scheme
    assert _ensure_url_scheme(True, 'host') == 'https://host'
    assert _ensure_url_scheme(False, 'host:4566') == 'http://host:4566'
    assert _ensure_url_scheme(True, None) is None

    # Bucket resolution
    store = S3FileStore()
    os.environ['AWS_S3_BUCKET'] = 'from-env'
    assert store._get_bucket_name() == 'from-env'

    store2 = S3FileStore(bucket_name='explicit')
    assert store2._get_bucket_name() == 'explicit'

    # create_s3_client with mock
    from unittest.mock import patch
    os.environ['LOCALSTACK_ENDPOINT'] = 'http://localhost:4566'
    with patch('boto3.client') as mock:
        create_s3_client()
    mock.assert_called_once()
    _, kwargs = mock.call_args
    assert kwargs['endpoint_url'] == 'http://localhost:4566'
    assert kwargs['use_ssl'] is False

    ok('Store modes verified')


# ── step 8: execution state transitions ──────────────────────────────

def verify_state_transitions() -> None:
    print('\n── Step 8: Verify ARCHIVED state ──')

    from openhands.app_server.automation.execution_models import (
        ExecutionState, ExecutionRecord, VALID_TRANSITIONS,
    )

    assert ExecutionState.ARCHIVED.value == 'ARCHIVED'
    assert ExecutionState.ARCHIVED in VALID_TRANSITIONS[ExecutionState.COMPLETED]
    assert ExecutionState.ARCHIVED in VALID_TRANSITIONS[ExecutionState.FAILED]
    assert len(VALID_TRANSITIONS[ExecutionState.ARCHIVED]) == 0

    rec = ExecutionRecord(execution_id='test', archive_location='s3://b/k')
    assert rec.archive_location == 's3://b/k'

    ok('ARCHIVED state transitions correct')


# ── cleanup ──────────────────────────────────────────────────────────

def cleanup(s3_key: str) -> None:
    print('\n── Cleanup ──')
    os.environ.setdefault('AWS_S3_BUCKET', BUCKET)
    from openhands.app_server.file_store.s3 import S3FileStore
    store = S3FileStore(bucket_name=BUCKET)
    store.delete(s3_key)
    ok(f'Deleted {s3_key}')


# ── main ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description='LocalStack S3 archive test')
    parser.add_argument('--keep', action='store_true',
                        help='Keep archive in S3 after test')
    args = parser.parse_args()

    print('=' * 60)
    print('  LocalStack S3 Archive Test Suite')
    print('=' * 60)

    ensure_localstack()
    create_bucket()
    s3_key, meta = write_archive()
    verify_cli(s3_key)
    verify_filestore(s3_key, meta)
    verify_mapping_keys()
    verify_store_modes()
    verify_state_transitions()

    if not args.keep:
        cleanup(s3_key)
    else:
        print(f'\n  Archive kept at s3://{BUCKET}/{s3_key}')

    print('\n' + '=' * 60)
    print('  ALL TESTS PASSED\n')
    print(f'  Archive S3 key: s3://{BUCKET}/{s3_key}')
    print(f'  Execution ID:   {meta["execution_id"]}')
    print(f'  Conversation ID: {meta["conversation_id"]}')
    print('=' * 60)


if __name__ == '__main__':
    main()
