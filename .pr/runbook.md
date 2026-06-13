# Deployment Runbook: Jira & GitHub Webhook Driven OpenHands Automation

## 1. Implementation Coverage Audit

### 1.1 Jira Webhook Flow
| Requirement | Status | File(s) |
|------------|--------|---------|
| POST /api/v1/webhooks/jira endpoint | ✅ Done | `jira_webhook_router.py` |
| HMAC-SHA256 signature verification | ✅ Done | `jira_automation_service.py:verify_jira_signature()` |
| Event type parsing (jira:issue_created) | ✅ Done | `jira_webhook_router.py:handle_jira_webhook()` |
| Issue data extraction (key, summary, description, type, priority, reporter, labels) | ✅ Done | `jira_automation_service.py:extract_jira_issue_data()` |
| Idempotency (event ID computation + dedup) | ✅ Done | `jira_automation_service.py:compute_jira_event_id()`, `execution_service.py:create_execution()` |
| Background processing (not in request) | ✅ Done | FastAPI `BackgroundTasks` |
| Branch name generation (deterministic, traceable) | ✅ Done | `jira_automation_service.py:generate_jira_branch_name()` |
| Execution record created | ✅ Done | `execution_service.py:create_execution()` |
| OpenHands conversation created | ✅ Done | `openhands_client.py:create_conversation()` |
| State transitions persisted | ✅ Done | `execution_store.py:update_state()` |

### 1.2 GitHub Webhook Flow
| Requirement | Status | File(s) |
|------------|--------|---------|
| POST /api/v1/webhooks/github endpoint | ✅ Done | `github_webhook_router.py` |
| HMAC-SHA256 signature verification | ✅ Done | `github_automation_service.py:verify_github_signature()` |
| PR review comment extraction | ✅ Done | `github_automation_service.py:extract_github_review_data()` |
| Idempotency (X-GitHub-Delivery header) | ✅ Done | `github_webhook_router.py` passes delivery_id |
| NEW conversation per review (no reuse) | ✅ Done | `openhands_client.py` creates new conversation each time |
| Future events (pull_request_review, issue_comment) | 🔲 Planned | Stub returns `ignored` for unsupported event types |

### 1.3 Execution Lifecycle
| Requirement | Status | File(s) |
|------------|--------|---------|
| States: RECEIVED → QUEUED → RUNNING → COMPLETED/FAILED/CANCELLED | ✅ Done | `execution_models.py` (Enum + state machine) |
| State transitions validated | ✅ Done | `execution_models.py:validate_transition()` |
| State transitions persisted | ✅ Done | `execution_store.py:update_state()` |
| Execution history queryable | ✅ Done | `execution_store.py:list_executions(), count_executions()` |
| execution_id generated per run | ✅ Done | `correlation.py:generate_execution_id()` |

### 1.4 Database Changes
| Requirement | Status | File(s) |
|------------|--------|---------|
| executions table | ✅ Done | `models.py:StoredExecution` + migration 011 |
| jira_issues table | ✅ Done | `models.py:StoredJiraIssue` + migration 011 |
| github_pull_requests table | ✅ Done | `models.py:StoredGitHubPullRequest` + migration 011 |
| review_iterations table | ✅ Done | `models.py:StoredReviewIteration` + migration 011 |
| Primary keys, foreign keys, indexes | ✅ Done | Migration 011 |
| Existing models modified? | ❌ No | Zero modifications to existing models |

### 1.5 OpenHands Integration
| Requirement | Status | File(s) |
|------------|--------|---------|
| Conversation creation via AppConversationService | ✅ Done | `openhands_client.py` |
| AUTOMATION trigger type | ✅ Done | `AppConversationTrigger.AUTOMATION` |
| EventCallbackProcessor for terminal state detection | ✅ Done | `callback_processors.py:AutomationEventCallbackProcessor` |
| Registered as processor in conversation start request | ✅ Done | `openhands_client.py` passes in `processors` list |
| Correlation IDs in conversation metadata | ✅ Done | `correlation.py:build_log_context()` |

### 1.6 Langfuse Integration
| Requirement | Status | File(s) |
|------------|--------|---------|
| Langfuse service (optional, no-op when not configured) | ✅ Done | `langfuse_service.py:LangfuseService` |
| Trace creation on execution start | ✅ Done | `execution_service.py` calls `langfuse.start_trace()` |
| Trace finalization on completion | ✅ Done | `callback_processors.py` calls `langfuse.finalize_trace()` |
| Captures execution_id, conversation_id, jira_issue_key, etc. | ✅ Done | `langfuse_service.py` metadata dict |
| Trace hierarchy (Run → Git Ops → Branch/Commit/PR) | 🟡 Partial | Root span created; sub-spans are design-ready for follow-up |
| Langfuse SDK dependency required? | ❌ No | Not added to pyproject.toml; pip install manually if needed |
| Environment variables documented | ✅ Done | See section 4.3 below |

### 1.7 Community Edition Compatibility
| Requirement | Status |
|------------|--------|
| Zero enterprise imports | ✅ All imports use `openhands.app_server.*` only |
| Zero enterprise code modifications | ✅ OSS files untouched (only `v1_router.py` had new imports added) |
| Uses OSS Base (sql_utils) | ✅ `models.py` inherits from `openhands.app_server.utils.sql_utils.Base` |
| Uses OSS session pattern | ✅ `execution_store.py` uses `get_global_config().db_session.get_async_session_maker()` |
| Uses OSS AppConversationService | ✅ `openhands_client.py` uses `get_app_conversation_service()` |
| No new dependencies added to pyproject.toml | ✅ Langfuse is optional, installed manually |
| All code lives outside enterprise/ | ✅ Package at `openhands/app_server/automation/` |

---

## 2. Database Migration

### 2.1 New Tables

Four new tables are created by migration `011`:

**executions** — Canonical execution lifecycle record
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key (auto) |
| execution_id | VARCHAR(255) | **UNIQUE** — format: `exec_{12_hex_chars}` |
| source_type | VARCHAR(50) | `jira` or `github` |
| source_event_id | VARCHAR(255) | **UNIQUE** — webhook event fingerprint for idempotency |
| state | VARCHAR(20) | `RECEIVED`, `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED` |
| jira_issue_key | VARCHAR(50) | e.g., `KAN-17` |
| github_pr_id | INTEGER | e.g., `42` |
| repository | VARCHAR(255) | e.g., `thatIsSharif/openhands` |
| branch | VARCHAR(255) | e.g., `feature/KAN-17-add-automation` |
| conversation_id | VARCHAR(255) | OpenHands conversation UUID |
| error_message | TEXT | Failure reason |
| started_at | DATETIME | When transitioned to RUNNING |
| completed_at | DATETIME | When transitioned to terminal state |
| created_at | DATETIME | Auto |
| updated_at | DATETIME | Auto |

**jira_issues** — Jira issue metadata
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key (auto) |
| issue_key | VARCHAR(50) | **UNIQUE** — e.g., `KAN-17` |
| summary | TEXT | Issue title |
| description | TEXT | Issue description |
| issue_type | VARCHAR(50) | `Story`, `Bug`, `Task`, etc. |
| priority | VARCHAR(50) | `Medium`, `High`, etc. |
| reporter | VARCHAR(255) | Display name |
| labels | TEXT[] | PostgreSQL array |
| webhook_event_id | VARCHAR(255) | **UNIQUE** — SHA-256 of webhook content |
| execution_id | INTEGER | FK to executions |
| created_at / updated_at | DATETIME | Auto |

**github_pull_requests** — GitHub PR tracking
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key (auto) |
| pr_number | INTEGER | e.g., `42` |
| repository | VARCHAR(255) | e.g., `owner/repo` |
| owner | VARCHAR(255) | Repository owner |
| branch | VARCHAR(255) | Head branch |
| title | TEXT | PR title |
| state | VARCHAR(20) | `open`, `closed`, `merged` |
| execution_id | INTEGER | FK to executions |
| pr_url | TEXT | Full GitHub URL |
| created_at / updated_at | DATETIME | Auto |

**review_iterations** — Review cycle tracking
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Primary key (auto) |
| execution_id | INTEGER | FK to executions |
| iteration_number | INTEGER | 1, 2, 3... |
| review_comment_id | BIGINT | GitHub comment ID |
| reviewer | VARCHAR(255) | GitHub username |
| comment_body | TEXT | Review comment text |
| pr_number | INTEGER | e.g., `42` |
| repository | VARCHAR(255) | e.g., `owner/repo` |
| created_at | DATETIME | Auto |

### 2.2 Migration Commands

```bash
# Run all pending migrations
cd /path/to/openhands
alembic upgrade head

# Run just the automation migration
alembic upgrade 011

# Verify current migration version
alembic current

# View migration history
alembic history

# Rollback (if needed)
alembic downgrade 010
```

**Note**: No manual SQL scripts are needed. No database recreation is required. The migration is additive (new tables only).

---

## 3. Application Startup

### 3.1 Environment Variables

#### Required
| Variable | Description | Default |
|----------|-------------|---------|
| `JIRA_WEBHOOK_SECRET` | HMAC-SHA256 secret for Jira webhook verification | _(none — verification skipped if empty)_ |
| `GITHUB_WEBHOOK_SECRET` | HMAC-SHA256 secret for GitHub webhook verification | _(none — verification skipped if empty)_ |

#### Optional (Langfuse Tracing)
| Variable | Description | Default |
|----------|-------------|---------|
| `LANGFUSE_PUBLIC_KEY` | Langfuse project public key | _(none — tracing disabled if empty)_ |
| `LANGFUSE_SECRET_KEY` | Langfuse project secret key | _(none — tracing disabled if empty)_ |
| `LANGFUSE_HOST` | Langfuse server URL | `https://cloud.langfuse.com` |

### 3.2 New API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/webhooks/jira` | Jira issue webhook receiver |
| POST | `/api/v1/webhooks/github` | GitHub PR review comment webhook receiver |

### 3.3 Dependencies
| Package | Required? | Notes |
|---------|-----------|-------|
| `langfuse` | No | Optional — only for Langfuse tracing. Install with `pip install langfuse` |

No new packages were added to `pyproject.toml`. Langfuse is optional and must be installed manually if tracing is desired.

### 3.4 Startup Commands

```bash
# Standard startup (no change needed — routers auto-register)
make run

# Or start just the backend
cd openhands
poetry run uvicorn openhands.server.listen:app --reload --port 3000

# Verify routers are loaded
curl http://localhost:3000/api/v1/openapi.json | jq '.paths | keys'
# Should include /api/v1/webhooks/jira and /api/v1/webhooks/github
```

---

## 4. Langfuse Integration Details

### 4.1 Architecture

Langfuse tracing is **optional**. The `LangfuseService` automatically checks for `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` environment variables. If either is missing, all methods are no-ops (no Langfuse SDK calls, no exceptions).

### 4.2 Trace Hierarchy

When tracing is enabled, each execution generates:

```
Trace: exec_{execution_id}
├── name: "Automation: {source_type}"
├── session_id: {execution_id}
├── input: {execution metadata}
└── Span: "OpenHands Run" (root span)
    └── Metadata: {full execution context}
```

Fields captured in trace metadata:
- `source_type` — `jira` or `github`
- `execution_id` — canonical execution ID
- `jira_issue_key` — (if from Jira)
- `github_pr_id` — (if from GitHub)
- `repository` — repository full name
- `branch` — target branch
- `status` — final execution state
- `conversation_id` — OpenHands conversation UUID
- `error_message` — (if failed)
- `duration_seconds` — execution wall-clock time

### 4.3 What Events Create Traces

| Event | Trace Action |
|-------|-------------|
| Execution transitions to RUNNING | `LangfuseService.start_trace()` — creates trace + root span |
| Conversation reaches terminal state (FINISHED/ERROR/STUCK) | `AutomationEventCallbackProcessor` calls `LangfuseService.finalize_trace()` — adds output/status metadata |

### 4.4 How to Verify Traces

```bash
# 1. Verify Langfuse is configured
echo $LANGFUSE_PUBLIC_KEY  # Should be non-empty
echo $LANGFUSE_SECRET_KEY  # Should be non-empty

# 2. Check logs for Langfuse initialization
grep "Langfuse tracing" /var/log/openhands/app.log
# Expected: "[Automation] Langfuse tracing enabled"

# 3. Simulate a webhook event and check Langfuse dashboard
# Log into your Langfuse project → Traces → search for exec_*

# 4. Verify trace metadata in logs
grep "Langfuse trace created" /var/log/openhands/app.log
# Expected: "[Automation] Langfuse trace created: exec_<id>"
```

### 4.5 Required Langfuse Setup

1. Create a Langfuse account at https://cloud.langfuse.com (or self-host)
2. Create a project
3. Copy API keys from Project Settings → API Keys
4. Set env vars and restart the server

---

## 5. Validation Checklist

### 5.1 Prerequisites
```bash
# 1. Verify database migration
alembic upgrade head
alembic current
# Should show: 011 (current)

# 2. Verify server starts
make run
# Check for: "Application startup complete"

# 3. Verify endpoints registered
curl http://localhost:3000/api/v1/openapi.json | jq '.paths | keys'
# Expected: "/api/v1/webhooks/jira" and "/api/v1/webhooks/github"

# 4. Verify no import errors
cd openhands && python3 -c "
from openhands.app_server.automation.models import StoredExecution;
from openhands.app_server.automation.execution_models import ExecutionState, SourceType;
from openhands.app_server.automation.execution_store import ExecutionStore;
from openhands.app_server.automation.execution_service import ExecutionService;
from openhands.app_server.automation.correlation import generate_execution_id, build_log_context;
from openhands.app_server.automation.openhands_client import OpenHandsClient;
from openhands.app_server.automation.callback_processors import AutomationEventCallbackProcessor;
from openhands.app_server.automation.langfuse_service import LangfuseService;
from openhands.app_server.automation.jira_automation_service import JiraAutomationService, verify_jira_signature;
from openhands.app_server.automation.github_automation_service import GitHubAutomationService, verify_github_signature;
print('All automation module imports OK!')
"
```

### 5.2 Jira Webhook Test
```bash
# Simulate a Jira webhook event
curl -X POST http://localhost:3000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d '{
    "webhookEvent": "jira:issue_created",
    "issue": {
      "key": "KAN-17",
      "fields": {
        "summary": "Test issue",
        "description": "This is a test",
        "issuetype": {"name": "Story"},
        "priority": {"name": "Medium"},
        "reporter": {"displayName": "TestUser"},
        "labels": ["test"]
      }
    },
    "timestamp": 1234567890
  }'

# Check response
# Expected: {"status": "accepted", "issue_key": "KAN-17"}
# (or {"status": "duplicate"} if same event sent again)

# Check logs for background processing
grep "Jira event processed" /var/log/openhands/app.log
# Expected: "[Automation] Jira event processed: running ..."

# Check database for execution record
echo "SELECT execution_id, state, jira_issue_key, branch FROM executions;" | \
  psql $DATABASE_URL
```

### 5.3 GitHub Webhook Test
```bash
# Simulate a GitHub PR review comment webhook
curl -X POST http://localhost:3000/api/v1/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request_review_comment" \
  -H "X-GitHub-Delivery: test-delivery-001" \
  -d '{
    "action": "created",
    "repository": {
      "full_name": "thatIsSharif/openhands",
      "owner": {"login": "thatIsSharif"},
      "name": "openhands"
    },
    "pull_request": {
      "number": 42,
      "head": {"ref": "feature/test"},
      "base": {"ref": "main"},
      "title": "Test PR",
      "body": "Test body"
    },
    "comment": {
      "id": 12345,
      "body": "Please fix this issue"
    },
    "sender": {
      "login": "reviewer1"
    }
  }'

# Check response
# Expected: {"status": "accepted"}
# (or {"status": "duplicate"} if same delivery sent again)

# Check logs
grep "GitHub event processed" /var/log/openhands/app.log
# Expected: "[Automation] GitHub event processed: running ..."
```

### 5.4 Idempotency Test
```bash
# Jira: send the same event twice
curl -X POST http://localhost:3000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d '{"webhookEvent": "jira:issue_created", "issue": {"key": "IDEMP-1"}, "timestamp": 1111111}'
# First call → {"status": "accepted"}

curl -X POST http://localhost:3000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -d '{"webhookEvent": "jira:issue_created", "issue": {"key": "IDEMP-1"}, "timestamp": 1111111}'
# Second call → {"status": "duplicate", "execution_id": "<same_exec_id>"}
```

### 5.5 Execution State Verification
```bash
# Check state transitions in database
echo "
SELECT execution_id, state, source_type, jira_issue_key, 
       started_at, completed_at, error_message
FROM executions ORDER BY created_at DESC LIMIT 5;
" | psql $DATABASE_URL

# Expected: execution progresses through states
# RECEIVED → QUEUED → RUNNING → COMPLETED (or FAILED)
```

### 5.6 Langfuse Trace Verification (if configured)
```bash
# 1. Check logs for trace creation
grep "Langfuse trace" /var/log/openhands/app.log

# 2. Check Langfuse dashboard
# Visit https://cloud.langfuse.com → Traces → search for exec_*

# 3. Verify trace has correct metadata
# Each trace should contain: source_type, execution_id, status,
# and optionally: jira_issue_key, github_pr_id, conversation_id
```

### 5.7 Correlation ID Verification
```bash
# Check structured logs
grep "execution_id" /var/log/openhands/app.log | tail -5

# Every log line should include:
# - execution_id
# - conversation_id (if available)
# - jira_issue_key (if from Jira)
# - pr_number (if from GitHub)
# - repository (if available)
# - branch (if available)
```

---

## 6. File Inventory

### New Files (openhands/app_server/automation/)
```
automation/
├── __init__.py                      # Package init
├── callback_processors.py           # AutomationEventCallbackProcessor (EventCallbackProcessor subclass)
├── correlation.py                   # Execution ID generation, log context builder
├── execution_models.py              # ExecutionState enum, SourceType enum, validate_transition()
├── execution_service.py             # Business logic with idempotency + Langfuse hooks
├── execution_store.py               # SQLAlchemy CRUD operations (session-per-operation)
├── github_automation_service.py     # GitHub webhook processing service
├── github_webhook_router.py         # POST /api/v1/webhooks/github
├── jira_automation_service.py       # Jira webhook processing service
├── jira_webhook_router.py           # POST /api/v1/webhooks/jira
├── langfuse_service.py              # Optional Langfuse tracing (no-op when not configured)
├── models.py                        # SQLAlchemy ORM models (StoredExecution, etc.)
└── openhands_client.py              # AppConversationService wrapper
```

### New Files (database migration)
```
openhands/app_server/app_lifespan/alembic/versions/
└── 011_add_automation_tables.py     # Add executions, jira_issues, github_pull_requests, review_iterations
```

### Modified Files
```
openhands/app_server/v1_router.py    # Registered automation webhook routers (+2 lines)
```

### New Test Files
```
tests/unit/test_automation/
├── __init__.py
├── test_correlation.py
├── test_execution_models.py
├── test_execution_service.py
├── test_github_automation.py
└── test_jira_automation.py
```

### Design Documents
```
.pr/
├── design.md                        # Architecture analysis and design
└── runbook.md                       # This file — deployment runbook
```

---

## 7. Failure Recovery

### 7.1 OpenHands Agent Failure
- **Symptom**: Conversation reaches ERROR or STUCK status
- **Detection**: `AutomationEventCallbackProcessor` detects terminal state via `ConversationStateUpdateEvent`
- **Action**: Execution state → FAILED, error_message persisted, Langfuse trace finalized
- **Recovery**: Manual — check logs, fix the issue, re-trigger via webhook (idempotency ensures no duplicate)

### 7.2 GitHub API Failure
- **Symptom**: Agent cannot push branch or update PR
- **Detection**: Error returned to OpenHands conversation
- **Action**: Conversation may fail → execution FAILED
- **Recovery**: Ensure GITHUB_TOKEN has proper permissions (contents:write, pull_requests:write)

### 7.3 Jira API Failure
- **Symptom**: Cannot read Jira issue details
- **Detection**: Error during webhook processing
- **Action**: Execution FAILED with error message
- **Recovery**: Ensure JIRA_* env vars are correct, Jira webhook secret matches

### 7.4 Database Connection Failure
- **Symptom**: Execution creation or state update fails
- **Detection**: Error logged, empty response returned
- **Action**: Webhook returns 500, no execution record created
- **Recovery**: Fix database connectivity, ensure DATABASE_URL is correct, re-send webhook

### 7.5 Langfuse Failure
- **Symptom**: Langfuse SDK raises unexpected exception
- **Detection**: Warning logged (not error)
- **Action**: Graceful degradation — execution proceeds normally, Langfuse trace is skipped
- **Recovery**: Langfuse failures never affect execution lifecycle

---

## 8. Upstream Testing Requirements

To run the unit tests for the automation platform:

```bash
cd /path/to/openhands

# Run all automation tests
poetry run pytest tests/unit/test_automation/ -v

# Run specific test file
poetry run pytest tests/unit/test_automation/test_execution_models.py -v

# Run with coverage
poetry run pytest tests/unit/test_automation/ --cov=openhands.app_server.automation -v
```

Test coverage includes:
- State machine transition validation (all valid + invalid paths)
- Webhook signature verification (Jira + GitHub, valid + invalid + missing)
- Event ID computation and idempotency
- Issue/review data extraction (all fields, missing fields, edge cases)
- Branch name generation (feature, bugfix, truncation, special characters)
- Correlation ID generation (format, uniqueness)
- Execution service (create, duplicate detection, transitions, not-found)
