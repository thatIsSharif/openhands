"""Unit tests for the workspace export/restore system."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands.app_server.workspace_export.export_service import (
    WorkspaceExportService,
)
from openhands.app_server.workspace_export.local_storage import (
    LocalStorage,
    _sanitize_jira_key as sanitize_jira_key,
)
from openhands.app_server.workspace_export.restore_service import (
    WorkspaceRestoreService,
)
from openhands.app_server.workspace_export.storage_backend import (
    SnapshotMetadata,
    StorageBackend,
)

# ======================================================================
# LocalStorage tests
# ======================================================================


@pytest.fixture
def temp_export_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def storage(temp_export_dir):
    return LocalStorage(export_dir=str(temp_export_dir))


@pytest.fixture
def sample_metadata():
    return SnapshotMetadata(
        jira_key='TEST-123',
        snapshot_tag='oh-export-test-123',
        created_at='1700000000',
    )


@pytest.mark.asyncio
async def test_local_storage_save_snapshot(storage, sample_metadata):
    """Verify that saving a snapshot creates all three artifact files."""
    ok = await storage.save_snapshot(
        jira_key='TEST-123',
        image_tar=b'fake-tar-data',
        conversation_json='{"key": "value"}',
        metadata=sample_metadata,
    )
    assert ok is True

    # Check that files were created
    conv_dir = storage._conversation_dir('TEST-123')
    assert (conv_dir / 'metadata.json').exists()
    assert (conv_dir / 'conversation.json').exists()
    assert (conv_dir / 'snapshot.tar').exists()

    # Verify contents
    with open(conv_dir / 'metadata.json') as f:
        md = json.load(f)
    assert md['jira_key'] == 'TEST-123'
    assert md['snapshot_tag'] == 'oh-export-test-123'

    with open(conv_dir / 'conversation.json') as f:
        assert f.read() == '{"key": "value"}'

    with open(conv_dir / 'snapshot.tar', 'rb') as f:
        assert f.read() == b'fake-tar-data'


@pytest.mark.asyncio
async def test_local_storage_exists_and_load(storage, sample_metadata):
    """Verify exists(), load_image_tar(), load_conversation_json(), load_metadata()."""
    await storage.save_snapshot(
        jira_key='TEST-456',
        image_tar=b'tar-data',
        conversation_json='{"conversation_id": "abc"}',
        metadata=sample_metadata,
    )

    assert await storage.exists('TEST-456') is True
    assert await storage.exists('NONEXISTENT') is False

    tar = await storage.load_image_tar('TEST-456')
    assert tar == b'tar-data'

    conv = await storage.load_conversation_json('TEST-456')
    assert conv == '{"conversation_id": "abc"}'

    md = await storage.load_metadata('TEST-456')
    assert md is not None
    assert md.jira_key == 'TEST-123'


@pytest.mark.asyncio
async def test_local_storage_load_nonexistent(storage):
    """Verify that loading nonexistent keys returns None."""
    assert await storage.load_image_tar('NOPE') is None
    assert await storage.load_conversation_json('NOPE') is None
    assert await storage.load_metadata('NOPE') is None


@pytest.mark.asyncio
async def test_local_storage_delete(storage, sample_metadata):
    """Verify delete() removes the directory."""
    await storage.save_snapshot(
        jira_key='TO-DEL',
        image_tar=b'x',
        conversation_json='{}',
        metadata=sample_metadata,
    )
    assert await storage.exists('TO-DEL') is True

    ok = await storage.delete('TO-DEL')
    assert ok is True
    assert await storage.exists('TO-DEL') is False


@pytest.mark.asyncio
async def test_local_storage_sanitize_jira_key(storage):
    """Verify that special characters in Jira keys are sanitized for filenames."""
    assert sanitize_jira_key('TEST-123') == 'TEST-123'
    assert sanitize_jira_key('TEST/PROJ-1') == 'TEST_PROJ-1'
    assert sanitize_jira_key('  ') != ''
    assert len(sanitize_jira_key('  ')) > 0


# ======================================================================
# SnapshotMetadata tests
# ======================================================================


def test_snapshot_metadata_defaults():
    """Verify SnapshotMetadata has sensible defaults."""
    md = SnapshotMetadata(
        jira_key='TST-1',
        snapshot_tag='tag',
        created_at='0',
    )
    assert md.export_version == '1.0'
    assert md.conversation_id is None
    assert md.llm_model is None
    assert md.sandbox_id is None


# ======================================================================
# WorkspaceExportService tests
# ======================================================================


@pytest.fixture
def mock_sandbox_service():
    svc = MagicMock()
    svc.snapshot_sandbox = AsyncMock(return_value='oh-export-tag-1')
    svc.delete_sandbox = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def mock_app_conversation_service():
    svc = MagicMock()
    svc.export_conversation = AsyncMock(return_value=b'fake-zip-content')
    return svc


@pytest.fixture
def mock_app_conversation_info_service():
    svc = MagicMock()
    info = MagicMock()
    info.sandbox_id = 'sandbox-123'
    info.llm_model = 'gpt-4'
    info.jira_issue_key = 'TEST-789'
    svc.get_app_conversation_info = AsyncMock(return_value=info)
    return svc


@pytest.fixture
def export_service(storage):
    return WorkspaceExportService(
        storage_backend=storage,
        docker_client=MagicMock(),
        max_image_size_mb=100,
    )


@pytest.mark.asyncio
async def test_export_service_empty_jira_key(export_service):
    """Verify export is skipped for empty Jira keys."""
    from uuid import UUID

    result = await export_service.export_conversation(
        conversation_id=UUID(int=1),
        jira_key='',
        app_conversation_service=MagicMock(),
        app_conversation_info_service=MagicMock(),
        docker_sandbox_service=MagicMock(),
    )
    assert result.success is False
    assert 'empty' in (result.error_message or '').lower()


@pytest.mark.asyncio
async def test_export_service_no_sandbox(
    export_service, mock_app_conversation_info_service
):
    from uuid import UUID

    info_without_sandbox = MagicMock()
    info_without_sandbox.sandbox_id = None
    info_without_sandbox.jira_issue_key = 'TEST-789'
    mock_app_conversation_info_service.get_app_conversation_info = AsyncMock(
        return_value=info_without_sandbox
    )

    result = await export_service.export_conversation(
        conversation_id=UUID(int=1),
        jira_key='TEST-789',
        app_conversation_service=MagicMock(),
        app_conversation_info_service=mock_app_conversation_info_service,
        docker_sandbox_service=MagicMock(),
    )
    assert result.success is False
    assert 'no sandbox' in (result.error_message or '').lower()


@pytest.mark.asyncio
async def test_export_service_happy_path(
    temp_export_dir, mock_sandbox_service, mock_app_conversation_service,
    mock_app_conversation_info_service,
):
    """Test the full export flow with mocked Docker."""
    storage = LocalStorage(export_dir=str(temp_export_dir))
    mock_docker = MagicMock()
    mock_image = MagicMock()
    mock_image.save.return_value = [b'tar-chunk-1', b'tar-chunk-2']
    mock_docker.images.get.return_value = mock_image

    svc = WorkspaceExportService(
        storage_backend=storage,
        docker_client=mock_docker,
        max_image_size_mb=100,
    )

    from uuid import UUID

    result = await svc.export_conversation(
        conversation_id=UUID(int=42),
        jira_key='TEST-789',
        app_conversation_service=mock_app_conversation_service,
        app_conversation_info_service=mock_app_conversation_info_service,
        docker_sandbox_service=mock_sandbox_service,
    )

    assert result.success is True
    assert result.snapshot_tag is not None

    # Verify the snapshot was persisted
    assert await storage.exists('TEST-789') is True
    mock_sandbox_service.snapshot_sandbox.assert_awaited_once_with('sandbox-123')
    mock_sandbox_service.delete_sandbox.assert_awaited_once()


@pytest.mark.asyncio
async def test_export_service_snapshot_failure(
    export_service, mock_app_conversation_info_service, storage
):
    """Verify export handles snapshot failure gracefully."""
    sandbox = MagicMock()
    sandbox.snapshot_sandbox = AsyncMock(return_value=None)
    sandbox.delete_sandbox = AsyncMock()

    result = await export_service.export_conversation(
        conversation_id=MagicMock(),
        jira_key='TEST-FAIL',
        app_conversation_service=MagicMock(),
        app_conversation_info_service=mock_app_conversation_info_service,
        docker_sandbox_service=sandbox,
    )
    assert result.success is False
    assert 'snapshot' in (result.error_message or '').lower()


# ======================================================================
# WorkspaceRestoreService tests
# ======================================================================


@pytest.fixture
def restore_service(storage):
    return WorkspaceRestoreService(storage_backend=storage, docker_client=MagicMock())


@pytest.mark.asyncio
async def test_restore_service_no_snapshot(restore_service):
    """Verify restore returns clean 'no snapshot' result when none exists."""
    sandbox = MagicMock()
    sandbox.restore_from_snapshot = AsyncMock()

    result = await restore_service.restore_conversation(
        jira_key='NONEXISTENT',
        new_sandbox_id='sandbox-new',
        docker_sandbox_service=sandbox,
    )
    assert result.success is False
    assert 'no snapshot' in (result.error_message or '').lower()
    sandbox.restore_from_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_restore_service_happy_path(
    temp_export_dir, storage, sample_metadata
):
    """Test full restore flow with mocked Docker."""
    # Pre-save a snapshot
    await storage.save_snapshot(
        jira_key='TEST-RESTORE',
        image_tar=b'restore-tar-data',
        conversation_json='{"conversation_id": "restored"}',
        metadata=sample_metadata,
    )

    mock_docker = MagicMock()
    svc = WorkspaceRestoreService(
        storage_backend=storage, docker_client=mock_docker
    )

    sandbox = MagicMock()
    sandbox_info = MagicMock()
    sandbox_info.id = 'restored-sandbox-1'
    sandbox.restore_from_snapshot = AsyncMock(return_value=sandbox_info)

    result = await svc.restore_conversation(
        jira_key='TEST-RESTORE',
        new_sandbox_id='sandbox-target',
        docker_sandbox_service=sandbox,
    )

    assert result.success is True
    assert result.sandbox_info is not None
    sandbox.restore_from_snapshot.assert_awaited_once()

    # Verify docker load was called
    call_args = mock_docker.images.load.call_args
    assert call_args is not None
    assert call_args[0][0] == b'restore-tar-data'


@pytest.mark.asyncio
async def test_restore_service_restore_fails(
    temp_export_dir, storage, sample_metadata
):
    """Verify restore handles docker restore_from_snapshot failure."""
    await storage.save_snapshot(
        jira_key='TEST-RESTORE-FAIL',
        image_tar=b'tar-data',
        conversation_json='{}',
        metadata=sample_metadata,
    )

    mock_docker = MagicMock()
    svc = WorkspaceRestoreService(
        storage_backend=storage, docker_client=mock_docker
    )

    sandbox = MagicMock()
    sandbox.restore_from_snapshot = AsyncMock(return_value=None)

    result = await svc.restore_conversation(
        jira_key='TEST-RESTORE-FAIL',
        new_sandbox_id='sandbox-target',
        docker_sandbox_service=sandbox,
    )
    assert result.success is False
    assert 'none' in (result.error_message or '').lower()


# ======================================================================
# Edge Cases
# ======================================================================


@pytest.mark.asyncio
async def test_local_storage_concurrent_keys(storage, sample_metadata):
    """Verify that multiple Jira keys are isolated."""
    md2 = SnapshotMetadata(
        jira_key='OTHER-999',
        snapshot_tag='tag-999',
        created_at='1700000001',
    )

    await storage.save_snapshot(
        jira_key='TEST-001',
        image_tar=b'data-1',
        conversation_json='{}',
        metadata=sample_metadata,
    )
    await storage.save_snapshot(
        jira_key='OTHER-999',
        image_tar=b'data-2',
        conversation_json='{}',
        metadata=md2,
    )

    assert await storage.exists('TEST-001') is True
    assert await storage.exists('OTHER-999') is True

    tar1 = await storage.load_image_tar('TEST-001')
    tar2 = await storage.load_image_tar('OTHER-999')
    assert tar1 == b'data-1'
    assert tar2 == b'data-2'


@pytest.mark.asyncio
async def test_local_storage_empty_tar_is_valid(storage, sample_metadata):
    """An empty tar should be persisted correctly (edge case: small tmp file)."""
    ok = await storage.save_snapshot(
        jira_key='EMPTY',
        image_tar=b'',
        conversation_json='{}',
        metadata=sample_metadata,
    )
    assert ok is True
    tar = await storage.load_image_tar('EMPTY')
    assert tar == b''
