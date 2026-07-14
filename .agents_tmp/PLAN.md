# 1. OBJECTIVE

Implement a workspace export/restore system for OpenHands Community Version (self-hosted) that:
- Uses **Docker commit/save/load** to snapshot and restore the entire container filesystem
- Exports the full sandbox state when a conversation task completes (with Jira key)
- Deletes the Docker container to free ~600MB RAM per conversation
- Stores Docker image tars to local filesystem, organized by Jira issue key
- Restores snapshot from storage via `docker load` when the same Jira issue is assigned again
- Provides an S3-ready StorageBackend abstraction for future cloud storage
- Stores conversation history JSON alongside the image tar for continuity

# 2. CONTEXT SUMMARY

## Relevant System Components

### Docker Sandbox Snapshot Infrastructure (Already Exists!)
- **`DockerSandboxService.snapshot_sandbox()`** (line 564-604): Uses `docker commit` to create a full Docker image from a running/paused container. Returns a snapshot tag name (e.g. `oh-snapshot-<sandbox_id>-<timestamp>`).
- **`DockerSandboxService.restore_from_snapshot()`** (line 606-722): Takes a snapshot tag, creates + starts a new container from that image with proper ports/env/volumes. Returns `SandboxInfo`.
- **`DockerSandboxService.delete_sandbox()`** (line 724-763): Already calls `snapshot_sandbox()` before destroying the container (disaster recovery). Removes the container and its volume.
- The snapshot images stay on the Docker host but are NOT pushed to external storage — they're lost if Docker is pruned.

### Other Relevant Components
- **`LiveStatusAppConversationService`** (`openhands/app_server/app_conversation/live_status_app_conversation_service.py`): Orchestrates conversation lifecycle. `_start_app_conversation()` starts sandbox → sets up repo → runs skills → starts agent.
- **`AppConversationInfo`** (`openhands/app_server/app_conversation/app_conversation_models.py`): Already has `jira_issue_key: str | None` field and `tags: dict[str, str]`.
- **Event callback system** (`openhands/app_server/event_callback/`): `EventCallbackProcessor` subclasses triggered by event types (e.g., `SetTitleCallbackProcessor` auto-titles conversations).
- **`LocalFileStore`** (`openhands/app_server/file_store/local.py`): Atomic file write abstraction for persisting settings.
- **Existing export endpoint**: `GET /api/v1/app-conversations/{id}/download` exports conversation events+metadata as ZIP.

## Constraints

- **Must work with Community Version only** — no OpenHands Cloud/Enterprise features.
- **Local storage first**, S3-replaceable via a pluggable `StorageBackend`.
- **Must not break existing OpenHands functionality** — additive changes only.
- Jira issue key is already supported in `AppConversationInfo.jira_issue_key` and `AppConversationStartRequest.jira_issue_key`.

# 3. APPROACH OVERVIEW

We use **Docker's built-in commit/save/load mechanism** as the core export/restore engine, wrapping it with a pluggable storage layer and conversation-lifecycle hooks.

## How it works end-to-end

### Export (task complete → snapshot → store → delete container)
```
1. Agent finishes task → StopEvent fires ExportOnCompletionCallbackProcessor
2. Processor calls WorkspaceExportService.export(conversation_id, jira_key)
3. ExportService:
   a. Gets sandbox_id and container name from conversation info
   b. Calls docker commit → creates snapshot image (returns snapshot_tag)
   c. Calls docker save → streams image to tar bytes in memory
   d. Also fetches conversation export JSON via existing API
   e. Saves image tar + conversation JSON + metadata to StorageBackend
   f. On success: deletes container via delete_sandbox()
```

### Restore (new task → load snapshot → start container → continue)
```
1. User creates conversation with jira_issue_key
2. _start_app_conversation() detects jira_issue_key has stored snapshot
3. WorkspaceRestoreService:
   a. Loads image tar from StorageBackend
   b. Calls docker load to import the image into local Docker
   c. Calls restore_from_snapshot(snapshot_tag, new_sandbox_id)
   d. New container starts with ALL previous state preserved
4. Agent continues with full context (installed packages, files, config)
```

## Key Advantages Over File-by-File Export (Dropped Approach)

| Aspect | File-by-File (dropped) | Docker Snapshot (chosen) |
|--------|----------------------|------------------------|
| **State captured** | Only `working_dir` files | Everything: files, packages, binaries, config, temp |
| **Performance** | Slow for large trees (50k+ node_modules → N HTTP calls) | Fast: sequential I/O, single tar stream |
| **Agent-server needed** | Must be RUNNING for API calls | Container can be stopped/paused |
| **Existing code reused** | Minimal | `snapshot_sandbox()` + `restore_from_snapshot()` already exist |
| **Implementation complexity** | High (file enumeration, upload, per-file error handling) | Low (wrapper around existing infrastructure) |

## Architecture (3 services + 1 router)

1. **`StorageBackend` (ABC) + `LocalStorage` + `S3Storage` (stub)** — Pluggable interface for storing/loading Docker image tar bytes + metadata. Same abstract interface as before but simpler — stores a single large binary blob + JSON metadata per Jira key.

2. **`WorkspaceExportService`** — Orchestrates: `docker commit` → `docker save` → store tar → store conversation JSON → delete container.

3. **`WorkspaceRestoreService`** — Orchestrates: load tar from storage → `docker load` → `restore_from_snapshot()`.

4. **`ExportOnCompletionCallbackProcessor`** — Wires into existing event callback system to auto-export on agent stop/finish.

5. **New router** — Manual trigger endpoints for export/restore/status.

# 4. IMPLEMENTATION STEPS

## Step 1: Create the StorageBackend abstraction + LocalStorage + S3 stub

**Goal**: Provide a pluggable storage interface for Docker image tars + metadata with a local-filesystem implementation.

**Files to create**:
- `openhands/app_server/workspace_export/__init__.py`
- `openhands/app_server/workspace_export/storage_backend.py` — abstract `StorageBackend` class
- `openhands/app_server/workspace_export/local_storage.py` — `LocalStorage` implementation
- `openhands/app_server/workspace_export/s3_storage.py` — `S3Storage` stub

**StorageBackend interface**:
```python
class StorageBackend(ABC):
    async def save_snapshot(
        self, jira_key: str, image_tar: bytes, conversation_json: str, metadata: dict
    ) -> bool: ...
    async def load_image_tar(self, jira_key: str) -> bytes | None: ...
    async def load_conversation_json(self, jira_key: str) -> str | None: ...
    async def load_metadata(self, jira_key: str) -> dict | None: ...
    async def exists(self, jira_key: str) -> bool: ...
    async def delete(self, jira_key: str) -> bool: ...
```

**LocalStorage**: Filesystem layout under `WORKSPACE_EXPORT_DIR` (default: `~/.openhands/exports/`):
```
{export_dir}/conversations/{jira_key}/
├── metadata.json       # Jira key, timestamps, snapshot tag, LLM model, etc.
├── conversation.json   # Conversation events + metadata (from export API)
└── snapshot.tar        # Docker image tar (from docker save)
```

**S3Storage stub**: Raises `NotImplementedError` with TODO (ready for future S3 connection).

**File to update**:
- `openhands/app_server/server_config/server_config.py` — add `workspace_export_backend_class` config field (default: `LocalStorage`)

**Env var**: `WORKSPACE_EXPORT_DIR` (default: `~/.openhands/exports/`)

## Step 2: Create WorkspaceExportService

**Goal**: Orchestrate the full export flow — docker commit → docker save → store → delete container.

**File to create**:
- `openhands/app_server/workspace_export/export_service.py`

**Detailed logic**:

```
async def export_conversation(conversation_id: UUID, jira_key: str) -> ExportResult:
```

1. **Validate inputs** — `jira_key` must be non-empty
2. **Fetch conversation info** from `AppConversationInfoService` to get `sandbox_id`
3. **Get sandbox info** from `DockerSandboxService.get_sandbox()` — verify container exists
4. **Get the Docker container** via `docker_client.containers.get(container_name)`
5. **Commit the container** to a snapshot image:
   - Call `docker commit` on the container (the container can be running/paused/stopped)
   - Tag the image as `{snapshot_prefix}{jira_key}` (e.g., `oh-export-JIRA-123`)
   - This captures the complete filesystem, all installed packages, compiled binaries, etc.
6. **Save the image to bytes**:
   - Use `docker_client.images.get(tag)` then `image.save()` or the Docker SDK's equivalent
   - This streams the image layers into a tar archive in memory
   - Set size limit (e.g., 10GB) to prevent OOM
7. **Also export conversation JSON** via the existing `AppConversationService.export_conversation()` — extract conversation events as JSON
8. **Build metadata dict**: `{jira_key, conversation_id, snapshot_tag, exported_at, llm_model, title, agent_kind, sandbox_id, image_size_bytes}`
9. **Store via StorageBackend**: `storage.save_snapshot(jira_key, image_tar_bytes, conversation_json, metadata)`
10. **On storage success**: Delete the container via `DockerSandboxService.delete_sandbox(sandbox_id)` — this frees the ~600MB RAM
11. **Clean up the local snapshot image** from Docker (optional, to free disk space)
12. **Update conversation info** tags: `{exported_at: <iso_timestamp>}`
13. **Error handling**: If storage fails, DO NOT delete the container. Log and return error.

**Size management**: `WORKSPACE_EXPORT_MAX_IMAGE_SIZE_MB` env var (default: 5000). If the image tar exceeds this, fail with a clear error.

## Step 3: Create WorkspaceRestoreService

**Goal**: Check for existing snapshots, load + `docker load` + `restore_from_snapshot()`.

**File to create**:
- `openhands/app_server/workspace_export/restore_service.py`

**Detailed logic**:

```
async def restore_conversation(jira_key: str) -> SnapshotRestoreInfo | None:
```

1. Check `StorageBackend.exists(jira_key)` — if absent, return None (caller starts fresh)
2. **Load the image tar**: `storage.load_image_tar(jira_key)`
3. **Load metadata**: `storage.load_metadata(jira_key)` to get previous snapshot tag, LLM model, etc.
4. **Load conversation JSON**: `storage.load_conversation_json(jira_key)` for event replay
5. **Import into Docker**: Use `docker_client.images.load(image_tar_bytes)` — this loads the image into local Docker with its original tag
6. **Return** an object containing:
   - `snapshot_tag: str` — the tag to pass to `restore_from_snapshot()`
   - `conversation_json: str | None` — previous conversation events
   - `metadata: dict` — previous metadata for reference
7. The caller (`_start_app_conversation()` or the restore endpoint handler) then calls `DockerSandboxService.restore_from_snapshot(snapshot_tag, new_sandbox_id)` which creates the container and starts the agent-server.
8. **Error handling**: If `docker load` fails, clean up any partial import and return None.

## Step 4: Add event callback processor for auto-export on completion

**Goal**: Automatically export when a conversation task completes (agent stops / finishes).

**File to create**:
- `openhands/app_server/workspace_export/export_on_completion_processor.py`

**Logic**:
1. Create `ExportOnCompletionCallbackProcessor(EventCallbackProcessor)` that listens for `StopEvent`
2. On trigger:
   - Get `conversation_id` and `jira_issue_key` from the event context
   - If no `jira_issue_key`, skip (no-op — export only when Jira-tracked)
   - Call `WorkspaceExportService.export_conversation(conversation_id, jira_issue_key)`
   - Return success/failure result
3. **Registration**: In `_start_app_conversation()` in `LiveStatusAppConversationService`, automatically register this processor when `request.jira_issue_key` is set (alongside the existing `SetTitleCallbackProcessor`)

**File to update**:
- `openhands/app_server/app_conversation/live_status_app_conversation_service.py` — add the processor to the `processors` list when `jira_issue_key` is present (around lines 418-437)

## Step 5: Integrate restore into conversation start flow

**Goal**: When a conversation is started with a `jira_issue_key`, automatically restore from stored snapshot.

**File to update**:
- `openhands/app_server/app_conversation/live_status_app_conversation_service.py` — modify `_start_app_conversation()`

**Changes** (between lines ~270-285, before `_wait_for_sandbox_start`):
1. Check if `request.jira_issue_key` is set
2. If yes, call `WorkspaceRestoreService.restore_conversation(jira_issue_key)`
3. If a restore result is returned:
   - Extract `snapshot_tag` from the result
   - Call `DockerSandboxService.restore_from_snapshot(snapshot_tag, new_sandbox_id)` instead of starting a fresh sandbox
   - Skip the normal sandbox setup flow (no repo cloning, no setup script, no skill loading — the snapshot already has all of this)
   - Optionally load conversation JSON to replay events (or just mark the conversation for the UI)
4. If no restore result (no snapshot exists), proceed with normal fresh sandbox creation

**Important**: `restore_from_snapshot()` already handles port mappings, env vars, volumes, and network mode. The restored agent-server will be at a fresh URL with a fresh session API key. We extract that from the `SandboxInfo` and pass it along.

## Step 6: Add REST API endpoints for manual trigger

**Goal**: Allow manual export/restore/check/delete via API.

**File to create**:
- `openhands/app_server/workspace_export/export_restore_router.py`

**Endpoints**:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/workspace-exports/{conversation_id}` | Trigger export (body: `{jira_issue_key: str}`) |
| `POST` | `/api/v1/workspace-restores/{jira_issue_key}` | Trigger restore (returns new sandbox info) |
| `GET` | `/api/v1/workspace-exports/{jira_issue_key}` | Check if export exists + metadata |
| `DELETE` | `/api/v1/workspace-exports/{jira_issue_key}` | Delete stored export |

**File to update**:
- `openhands/app_server/v1_router.py` — add `export_restore_router` import and `include_router`

## Step 7: Configuration and wiring

**Goal**: Wire up all new components with configuration, dependency injection, and env vars.

**Env vars**:
| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_EXPORT_DIR` | `~/.openhands/exports/` | Local filesystem path for exports |
| `WORKSPACE_EXPORT_MAX_IMAGE_SIZE_MB` | `5000` | Max Docker image tar size (5GB) |
| `WORKSPACE_EXPORT_ENABLED` | `true` | Feature toggle |
| `WORKSPACE_EXPORT_SNAPSHOT_PREFIX` | `oh-export-` | Prefix for committed snapshot tags |

**Files to update**:
- `openhands/app_server/config.py` — Add factory functions / injectors for `StorageBackend`, `WorkspaceExportService`, `WorkspaceRestoreService`. Follow the existing pattern (e.g., `get_settings_store()`)
- `openhands/app_server/server_config/server_config.py` — Add `workspace_export_backend_class` with default pointing to `LocalStorage`

**Dependency wiring**:
```python
# In config.py
def get_workspace_export_storage_backend() -> StorageBackend:
    export_dir = os.getenv('WORKSPACE_EXPORT_DIR', str(Path.home() / '.openhands' / 'exports'))
    return LocalStorage(export_dir=export_dir)
```

## Step 8: Write unit tests

**Files to create**:
- `tests/unit/test_workspace_export/__init__.py`
- `tests/unit/test_workspace_export/test_storage_backend.py`
- `tests/unit/test_workspace_export/test_export_service.py`
- `tests/unit/test_workspace_export/test_restore_service.py`
- `tests/unit/test_workspace_export/test_processor.py`

**Testing approach**:
- **StorageBackend tests**: Use `tmp_path` fixture for real filesystem. Test save/load/exists/delete round-trips with a real tar file. Test overwrite behavior. Test concurrent access.
- **Export service tests**: Mock `DockerSandboxService`, `docker_client`, `AppConversationInfoService`. Test that `docker commit` → `docker save` → store → delete container sequence executes correctly. Test error paths (commit failure, save failure, storage failure, missing sandbox).
- **Restore service tests**: Mock `docker_client.images.load()`, `StorageBackend`. Test that tar is loaded from storage → `docker load` is called → restore info is returned. Test missing snapshot returns None.
- **Processor tests**: Mock `WorkspaceExportService`. Test that StopEvent triggers export call. Test that absence of `jira_issue_key` skips export.
- **Router tests**: Use `TestClient`. Test endpoints return correct status codes. Test 404 for missing conversation/export.

**Key edge cases to test**:
- Export fails mid-way → container is NOT deleted
- Image tar exceeds size limit → clear error, no container deletion
- Docker daemon unavailable → graceful error
- Multiple exports for same Jira key → last-write-wins
- Restore after storage cleared → fresh container starts

# 5. TESTING AND VALIDATION

## Unit Tests (pytest)

Run via:
```bash
poetry run pytest tests/unit/test_workspace_export/ -v
```

**Test scenarios**:

| Test | What it validates |
|------|------------------|
| `test_storage_save_and_load_image` | LocalStorage correctly saves and retrieves Docker image tar bytes |
| `test_storage_save_and_load_conversation` | Conversation JSON survives round-trip |
| `test_storage_exists_true` | `exists()` returns True when files present |
| `test_storage_exists_false` | `exists()` returns False when directory missing |
| `test_storage_delete_removes_all` | `delete()` removes entire directory tree |
| `test_storage_delete_nonexistent` | `delete()` on non-existent key is no-op (no error) |
| `test_export_happy_path` | Full flow: commit → save → store → delete container |
| `test_export_no_jira_key` | No-op when jira_issue_key is None |
| `test_export_commit_failure` | Commit fails → container NOT deleted |
| `test_export_storage_failure` | Storage fails → container NOT deleted, snapshot cleaned |
| `test_export_missing_sandbox` | Sandbox gone → graceful 404 |
| `test_export_image_too_large` | Exceeds size limit → clear error |
| `test_restore_happy_path` | Tar loaded → `docker load` called → restore info returned |
| `test_restore_no_export` | No stored export → returns None (fresh start) |
| `test_restore_load_failure` | `docker load` fails → returns None |
| `test_processor_triggers_on_stop` | StopEvent calls export_service.export() |
| `test_processor_skips_no_jira_key` | No jira_issue_key → no-op |
| `test_router_export_endpoint_200` | POST export returns 200 on success |
| `test_router_export_endpoint_404` | POST export with bad id returns 404 |
| `test_router_check_exist` | GET returns exist/not-exist status |
| `test_router_delete` | DELETE removes export and returns 200 |

## Integration Validation (Manual)

### Export Flow
1. Start a conversation with a Jira issue key (via API or UI with `jira_issue_key` set)
2. Let the agent complete its task (or trigger stop)
3. Verify export callback fires (check logs for "ExportOnCompletionCallbackProcessor" messages)
4. Verify export directory: `{export_dir}/conversations/{jira_key}/metadata.json`, `conversation.json`, `snapshot.tar`
5. Verify `docker ps -a` confirms the container was deleted
6. Verify `docker images` shows the snapshot image (with tag `oh-export-{jira_key}`)

### Restore Flow
1. Create a new conversation with the same Jira issue key
2. Verify in logs that `WorkspaceRestoreService` found the snapshot
3. Verify `docker load` imported the image
4. Verify a new container started from the snapshot via `restore_from_snapshot()`
5. Verify the agent-server starts and the workspace has all previous files
6. Verify the agent can continue working in the restored environment

### Edge Cases
- Export when container already stopped → commit works fine (container can be stopped)
- Restore with no snapshot → normal fresh sandbox starts
- Export while container still running agent → safe, commit captures in-flight state
- Target directory disk full → graceful error, container preserved
- Jira key with special characters → validated and sanitized

## Success Criteria

- [ ] Docker commit creates full snapshot of running/paused/stopped container
- [ ] Docker image tar is saved to local filesystem via StorageBackend
- [ ] Conversation JSON is stored alongside the image tar
- [ ] Container is deleted only after successful storage (atomicity)
- [ ] Docker load restores the image from local filesystem
- [ ] New container starts from restored image with full prior state
- [ ] All unit tests pass with >90% coverage on new code
- [ ] Feature is toggleable via `WORKSPACE_EXPORT_ENABLED` env var
- [ ] No regression in existing conversation lifecycle
- [ ] All pre-commit hooks pass (ruff, mypy, etc.)
