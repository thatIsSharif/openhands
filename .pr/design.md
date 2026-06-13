# Jira & GitHub Webhook Driven OpenHands Automation Platform

## Architecture Analysis & Design Document

> **Status:** Architecture Review
> **Jira:** KAN-17
> **Repository:** thatIsSharif/openhands

---

## Table of Contents

1. [Existing Architecture Analysis](#1-existing-architecture-analysis)
2. [High-Level Architecture Proposal](#2-high-level-architecture-proposal)
3. [Database Schema](#3-database-schema)
4. [Execution Lifecycle](#4-execution-lifecycle)
5. [API Design](#5-api-design)
6. [OpenHands Integration Plan](#6-openhands-integration-plan)
7. [Langfuse Observability Design](#7-langfuse-observability-design)
8. [Correlation & Traceability](#8-correlation--traceability)
9. [Idempotency Strategy](#9-idempotency-strategy)
10. [Branch Strategy](#10-branch-strategy)
11. [Failure Handling](#11-failure-handling)
12. [Implementation Plan](#12-implementation-plan)

---

## 1. Existing Architecture Analysis

### 1.1 FastAPI Routers

The codebase is organized as a **dual-repo** structure with an OSS core (`openhands/`) and an enterprise layer (`enterprise/`).

**OSS Routers** (`openhands/app_server/`):

| Router | Prefix | Purpose |
|--------|--------|---------|
| `v1_router.py` | `/api/v1` | Aggregates all sub-routers |
| `app_conversation_router.py` | (under `/api/v1`) | Conversation CRUD, streaming, task start |
| `event_router.py` | (under `/api/v1`) | Event search & listing |
| `git_router.py` | (under `/api/v1`) | Git installations, repos, branches |
| `webhook_router.py` | `/webhooks` | Event callbacks from sandbox |
| `secrets_router.py` | (under `/api/v1`) | Secrets CRUD |
| `sandbox_router.py` | (under `/api/v1`) | Sandbox lifecycle |
| `settings_router.py` | (under `/api/v1`) | LLM config, agent schema |
| `status_router.py` | `/api/v1` + root | Health endpoints |
| `user_router.py` | (under `/api/v1`) | User & skills management |

**Enterprise Routers** (`enterprise/server/routes/`):

| Router | Prefix | Purpose |
|--------|--------|---------|
| `integration/github.py` | `/integration` | GitHub webhook receiver |
| `integration/gitlab.py` | `/integration` | GitLab webhook receiver |
| `integration/bitbucket.py` | `/integration` | Bitbucket webhook receiver |
| `integration/jira.py` | `/integration/jira` | Jira Cloud webhook + OAuth |
| `integration/jira_dc.py` | `/integration/jira-dc` | Jira Data Center webhook |
| `integration/slack.py` | `/slack` | Slack events/interactions |

### 1.2 Service Layer Structure

Services follow an **abstract base class + concrete implementation** pattern:

- **`Manager[T]`** (ABC, `enterprise/integrations/manager.py`): Abstract integration manager with `receive_message()`, `send_message()`, `start_job()`
- **`AppConversationService`** (ABC, `openhands/app_server/app_conversation/`): Abstract conversation lifecycle service
- **`EventServiceBase`** (ABC): Abstract event storage
- **`AppLifespanService`** (ABC): Startup/shutdown lifecycle

### 1.3 Dependency Injection

Custom `Injector[T]` pattern (not solely FastAPI `Depends`):

```python
class Injector(Generic[T], ABC):
    async def inject(self, state, request) -> AsyncGenerator[T]: ...
    async def context(self, state, request) -> AsyncGenerator[T]: ...
    def depends(self, request) -> AsyncGenerator[T]: ...
```

- `DbSessionInjector` — SQLAlchemy async session, stored on `state.db_session`
- `HttpxClientInjector` — HTTPX async client
- `DiscriminatedUnionMixin` — config-driven service selection (e.g., `LiveStatusAppConversationServiceInjector` vs alternatives)

### 1.4 Background Job Processing

**No dedicated job queue** (no Celery, RQ, or similar).

Current patterns:
- **FastAPI `BackgroundTasks`**: Used by Jira webhook router to process messages asynchronously
- **`asyncio.create_task`**: Used by GitHub webhook router for automation event forwarding
- **`_consume_remaining` pattern**: Used by conversation creation endpoint to drain async generators in background

### 1.5 Database Access Layer

**Enterprise** uses a `Store` pattern:
```python
@dataclass
class JiraIntegrationStore:
    async def create_workspace(self, ...): ...
    async def get_workspace_by_name(self, name): ...
```

- Each method opens its own `async with a_session_maker() as session:`
- Models use SQLAlchemy 2.0 `Mapped` / `mapped_column` pattern
- Single shared `Base = DeclarativeBase` from `openhands/app_server/utils/sql_utils.py`

**OSS** defines models inline in service files (e.g., `StoredEventCallback` in `sql_event_callback_service.py`).

### 1.6 Existing GitHub Integration

**OSS** (`openhands/app_server/integrations/github/`):
- GraphQL client, app installations
- Service mixins: `repos.py`, `prs.py`, `branches_prs.py`, `features.py`, `resolver.py`

**Enterprise** (`enterprise/integrations/github/`):
- `GithubManager` — processes label-triggered events on issues/PRs
- `GithubV1CallbackProcessor` — posts summary back when conversation finishes
- Views: `GithubIssue`, `GithubIssueComment`, `GithubPRComment`, `GithubInlinePRComment`

**Current flow**: Label event → resolve issue/PR → create conversation → agent works → callback posts summary.

**Gap for this project**: No support for `pull_request_review_comment` webhook events. The current integration is resolver-based (label-triggered), not review-comment-triggered.

### 1.7 Existing Jira Integration

**Enterprise** (`enterprise/integrations/jira/`):
- `JiraManager` — OAuth-based workspace model
- `JiraV1CallbackProcessor` — posts summary back when conversation finishes
- `JiraNewConversationView` — creates conversation from Jira issue webhook

**Current flow**: Jira webhook → validate workspace → create conversation → agent works → callback posts comment.

**Gap for this project**: The existing Jira integration uses an OAuth workspace model that requires users to link accounts. The new automation platform needs a standalone webhook integration that doesn't require per-user OAuth linking — just webhook-based execution with execution records.

### 1.8 Observability

- **PostHog** (`openhands/analytics/`): Analytics service with consent gating
- **LiteLLM proxy tracing**: LLM call metadata passed to LiteLLM for Langfuse
- **No direct OpenTelemetry instrumentation**
- **No dedicated Langfuse SDK integration** in the codebase — Langfuse is configured externally via LiteLLM proxy

### 1.9 Key Architectural Patterns to Follow

1. **Store pattern**: `@dataclass` stores with per-method DB sessions
2. **Manager pattern**: `Manager[T]` ABC for integration management
3. **Callback processor**: `EventCallbackProcessor` for post-execution handling
4. **Dependency injection**: `Injector[T]` with `DiscriminatedUnionMixin`
5. **Webhook processing**: FastAPI `BackgroundTasks` for non-blocking responses
6. **Conversation creation**: Template rendering → `AppConversationStartRequest` → `start_app_conversation()`
7. **Alembic migrations**: Sequential revision IDs, shared `Base` metadata

---

## 2. High-Level Architecture Proposal

### 2.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            External Services                                │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │   Jira   │    │   GitHub     │    │   GitHub     │    │   OpenHands   │  │
│  │  Cloud   │    │  (Webhooks)  │    │   (API)      │    │   (Sandbox)   │  │
│  └────┬─────┘    └──────┬───────┘    └──────┬───────┘    └───────┬───────┘  │
└───────┼─────────────────┼───────────────────┼────────────────────┼──────────┘
        │                 │                   │                    │
┌───────┼─────────────────┼───────────────────┼────────────────────┼──────────┐
│       │                 │                   │                    │          │
│  ┌────▼─────────────────▼───────────────────┴────────────────────┴────┐     │
│  │                        FastAPI Application                          │     │
│  │                                                                     │     │
│  │  ┌──────────────────────────────────────────────────────────┐      │     │
│  │  │                    API Routers                            │      │     │
│  │  │  ┌──────────────┐  ┌──────────────────┐  ┌───────────┐  │      │     │
│  │  │  │ /api/v1/jira │  │ /api/v1/github   │  │ Existing  │  │      │     │
│  │  │  │ /webhook     │  │ /webhook         │  │ Routers   │  │      │     │
│  │  │  └──────┬───────┘  └───────┬──────────┘  └───────────┘  │      │     │
│  │  └─────────┼──────────────────┼─────────────────────────────┘      │     │
│  │            │                  │                                     │     │
│  │  ┌─────────▼──────────────────▼─────────────────────────────┐      │     │
│  │  │                   Service Layer                           │      │     │
│  │  │  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │      │     │
│  │  │  │JiraAutomation│  │GitHubAutomtn │  │  Execution     │  │      │     │
│  │  │  │  Service     │  │  Service     │  │  Service       │  │      │     │
│  │  │  └──────┬───────┘  └──────┬───────┘  └───────┬────────┘  │      │     │
│  │  └─────────┼──────────────────┼──────────────────┼───────────┘      │     │
│  │            │                  │                  │                   │     │
│  │  ┌─────────▼──────────────────▼──────────────────▼─────────────┐    │     │
│  │  │                    Store Layer (SQLAlchemy)                  │    │     │
│  │  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │    │     │
│  │  │  │JiraExecution │  │GithubExecutn │  │  OpenHands       │  │    │     │
│  │  │  │  Store      │  │    Store     │  │  Execution Store  │  │    │     │
│  │  │  └──────────────┘  └──────────────┘  └──────────────────┘  │    │     │
│  │  └─────────────────────────────────────────────────────────────┘    │     │
│  │                                                                     │     │
│  │  ┌──────────────────────────────────────────────────────────────┐   │     │
│  │  │                   Integration Services                       │   │     │
│  │  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │   │     │
│  │  │  │  OpenHands   │  │    GitHub    │  │      Jira        │   │   │     │
│  │  │  │  Client      │  │    Client    │  │     Client       │   │   │     │
│  │  │  └──────────────┘  └──────────────┘  └──────────────────┘   │   │     │
│  │  └──────────────────────────────────────────────────────────────┘   │     │
│  └─────────────────────────────────────────────────────────────────────┘     │
│                                                                               │
│  ┌────────────────────────────────────────────────────────────────────┐      │
│  │                     Database (PostgreSQL)                           │      │
│  │  ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌──────────────────┐   │      │
│  │  │executions│ │jira_issue│ │github_pull │ │review_iterations │   │      │
│  │  │          │ │          │ │_requests   │ │                  │   │      │
│  │  └──────────┘ └──────────┘ └────────────┘ └──────────────────┘   │      │
│  └────────────────────────────────────────────────────────────────────┘      │
└───────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Sequence Diagrams

#### Workflow 1: Jira Issue → OpenHands → Pull Request

```
Jira                  JiraWebhook           ExecutionService      OpenHandsClient       GitHub API
  │                        │                       │                    │                  │
  │  POST /webhook         │                       │                    │                  │
  │───────────────────────►│                       │                    │                  │
  │                        │                       │                    │                  │
  │  1. Validate signature │                       │                    │                  │
  │  2. Check idempotency  │                       │                    │                  │
  │  3. Parse payload      │                       │                    │                  │
  │                        │                       │                    │                  │
  │                        │  create_execution()   │                    │                  │
  │                        │──────────────────────►│  RECEIVED → QUEUED │                  │
  │                        │                       │────────────────────►                  │
  │  202 Accepted          │                       │                    │                  │
  │◄───────────────────────│                       │                    │                  │
  │                        │                       │                    │                  │
  │                        │   BackgroundTasks:    │                    │                  │
  │                        │   process_execution() │                    │                  │
  │                        │──────────────────────►│                    │                  │
  │                        │                       │  QUEUED → RUNNING  │                  │
  │                        │                       │────────────────────►                  │
  │                        │                       │                    │                  │
  │                        │                       │  create_conversation()                │
  │                        │                       │────────────────────►                  │
  │                        │                       │  clone_repo()       │                  │
  │                        │                       │────────────────────►                  │
  │                        │                       │  create_branch()    │                  │
  │                        │                       │──────────────────────────────────────►│
  │                        │                       │                    │                  │
  │                        │                       │  start_agent()     │                  │
  │                        │                       │────────────────────►                  │
  │                        │                       │                    │                  │
  │                        │                       │   [Agent executes]  │                  │
  │                        │                       │                    │                  │
  │                        │                       │  commit()          │                  │
  │                        │                       │──────────────────────────────────────►│
  │                        │                       │  create_pr()       │                  │
  │                        │                       │──────────────────────────────────────►│
  │                        │                       │                    │                  │
  │                        │                       │  RUNNING → COMPLETED                  │
  │                        │                       │────────────────────►                  │
  │                        │                       │                    │                  │
  │                        │                       │  Post comment with │                  │
  │                        │                       │  PR URL to Jira    │                  │
  │                        │                       │────────────────────►                  │
```

#### Workflow 2: GitHub Review Comments → OpenHands → PR Update

```
GitHub               GitHubWebhook          ExecutionService      OpenHandsClient       GitHub API
  │                        │                       │                    │                  │
  │  POST /webhook         │                       │                    │                  │
  │  (review_comment)      │                       │                    │                  │
  │───────────────────────►│                       │                    │                  │
  │                        │                       │                    │                  │
  │  1. Validate signature │                       │                    │                  │
  │  2. Check idempotency  │                       │                    │                  │
  │  3. Parse payload      │                       │                    │                  │
  │  4. Fetch PR context   │                       │                    │                  │
  │  5. Fetch reviews      │                       │                    │                  │
  │                        │                       │                    │                  │
  │                        │  create_execution()   │                    │                  │
  │                        │──────────────────────►│  RECEIVED → QUEUED │                  │
  │                        │                       │────────────────────►                  │
  │  202 Accepted          │                       │                    │                  │
  │◄───────────────────────│                       │                    │                  │
  │                        │                       │                    │                  │
  │                        │   BackgroundTasks:    │                    │                  │
  │                        │   process_execution() │                    │                  │
  │                        │──────────────────────►│  QUEUED → RUNNING  │                  │
  │                        │                       │────────────────────►                  │
  │                        │                       │                    │                  │
  │                        │                       │  create_NEW_conversation()            │
  │                        │                       │────────────────────►                  │
  │                        │                       │                    │                  │
  │                        │                       │  fetch PR diff     │                  │
  │                        │                       │──────────────────────────────────────►│
  │                        │                       │  fetch reviews     │                  │
  │                        │                       │──────────────────────────────────────►│
  │                        │                       │                    │                  │
  │                        │                       │  start_agent()     │                  │
  │                        │                       │  (with PR context  │                  │
  │                        │                       │   & review comments)│                  │
  │                        │                       │────────────────────►                  │
  │                        │                       │                    │                  │
  │                        │                       │   [Agent executes]  │                  │
  │                        │                       │                    │                  │
  │                        │                       │  commit to branch  │                  │
  │                        │                       │──────────────────────────────────────►│
  │                        │                       │                    │                  │
  │                        │                       │  RUNNING → COMPLETED                  │
  │                        │                       │────────────────────►                  │
```

### 2.3 Service Boundaries

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Execution Service                             │
│  - manage execution lifecycle (RECEIVED → ... → COMPLETED/FAILED)    │
│  - create execution records                                          │
│  - transition execution states                                       │
│  - query execution history                                           │
│  - idempotency checking                                              │
│  - correlation ID propagation                                        │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                     Jira Automation Service                          │
│  - validate Jira webhooks                                            │
│  - parse Jira payloads                                               │
│  - create execution records (via ExecutionService)                   │
│  - create OpenHands conversations                                    │
│  - manage branch/PR creation                                         │
│  - post status comments back to Jira                                 │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                    GitHub Automation Service                          │
│  - validate GitHub webhooks                                          │
│  - parse GitHub payloads (PR review comments)                        │
│  - fetch PR context, diff, unresolved reviews                        │
│  - create execution records (via ExecutionService)                   │
│  - create NEW OpenHands conversations for each review cycle          │
│  - commit to existing branch / update existing PR                    │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                      OpenHands Client                                │
│  - create conversations                                              │
│  - start task execution                                              │
│  - monitor conversation status                                       │
│  - check execution results                                           │
│  - request agent summaries                                           │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.4 Data Flow

```
Webhook Reception
     │
     ▼
Validation (signature, idempotency)
     │
     ▼
Execution Record Created (state=RECEIVED)
     │
     ▼
Execution Queued (state=QUEUED)
     │
     ▼
Background Processing (FastAPI BackgroundTasks)
     │
     ├── Jira: Parse issue → Create conversation → Agent executes
     │         → Create branch → Commit → Create PR → Post to Jira
     │
     └── GitHub: Parse review → Fetch PR context → Create conversation
                 → Agent executes → Commit to branch → Update PR
     │
     ▼
Execution Completed (state=COMPLETED) or Failed (state=FAILED)
     │
     ▼
Callback: Post results back to origin platform
```

### 2.5 Why This Fits the Existing Codebase

1. **Service layer pattern**: Mirrors existing `GithubManager`/`JiraManager` pattern in `enterprise/integrations/`
2. **Store pattern**: Reuses existing `@dataclass` store pattern from `enterprise/storage/`
3. **Background processing**: Uses FastAPI `BackgroundTasks` consistent with existing Jira webhook router
4. **Conversation creation**: Reuses `AppConversationStartRequest` / `start_app_conversation()` pattern
5. **Callback processor**: Extends `EventCallbackProcessor` for execution completion handling
6. **Dependency injection**: Follows existing `Injector[T]` pattern if needed
7. **Alembic migrations**: Follows existing sequential revision pattern
8. **Git provider tokens**: Reuses existing token management from `token_manager`

---

## 3. Database Schema

### 3.1 Entity Relationship Diagram

```
jira_issues
    │
    │  issue_key (PK, unique)
    │  summary
    │  description
    │  issue_type
    │  priority
    │  reporter
    │  labels
    │  webhook_event_id (unique, idempotency)
    │  created_at
    │  updated_at
    │
    └─── 1 ──── * ──── executions
                        │
                        │  id (PK, UUID)
                        │  execution_id (unique, correlation ID)
                        │  source_type (jira|github)
                        │  source_event_id (idempotency key)
                        │  state (enum)
                        │  jira_issue_key (FK → jira_issues)
                        │  github_pr_id (FK → github_pull_requests)
                        │  repository
                        │  branch
                        │  conversation_id
                        │  error_message
                        │  started_at
                        │  completed_at
                        │  created_at
                        │  updated_at
                        │
                        ├─── 1 ──── * ──── review_iterations
                        │                    │
                        │                    │  id (PK)
                        │                    │  execution_id (FK)
                        │                    │  iteration_number
                        │                    │  review_comment_id
                        │                    │  reviewer
                        │                    │  comment_body
                        │                    │  created_at
                        │                    │
                        └─── 1 ──── 1 ──── conversations
                                             │
                                             │  id (PK, UUID)
                                             │  execution_id (FK)
                                             │  openhands_conversation_id
                                             │  status
                                             │  model_used
                                             │  token_count
                                             │  cost
                                             │  started_at
                                             │  completed_at
                                             │  created_at

github_pull_requests
    │
    │  id (PK)
    │  pr_number
    │  repository
    │  owner
    │  branch
    │  title
    │  state (open|closed|merged)
    │  execution_id (FK → executions)
    │  pr_url
    │  created_at
    │  updated_at
    │
    └─── 1 ──── * ──── review_iterations
```

### 3.2 Table Definitions

#### `jira_issues`

```sql
CREATE TABLE jira_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_key VARCHAR(50) NOT NULL UNIQUE,
    summary TEXT NOT NULL,
    description TEXT,
    issue_type VARCHAR(50),
    priority VARCHAR(50),
    reporter VARCHAR(255),
    labels TEXT[],
    webhook_event_id VARCHAR(255) UNIQUE,
    execution_id UUID REFERENCES executions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_jira_issues_issue_key ON jira_issues(issue_key);
CREATE INDEX idx_jira_issues_webhook_event_id ON jira_issues(webhook_event_id);
```

#### `executions`

```sql
CREATE TYPE execution_state AS ENUM (
    'RECEIVED', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'
);

CREATE TABLE executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    execution_id VARCHAR(255) NOT NULL UNIQUE,
    source_type VARCHAR(50) NOT NULL,
    source_event_id VARCHAR(255),
    state execution_state NOT NULL DEFAULT 'RECEIVED',
    jira_issue_key VARCHAR(50) REFERENCES jira_issues(issue_key),
    github_pr_id UUID REFERENCES github_pull_requests(id),
    repository VARCHAR(255),
    branch VARCHAR(255),
    conversation_id UUID,
    error_message TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_executions_execution_id ON executions(execution_id);
CREATE INDEX idx_executions_state ON executions(state);
CREATE INDEX idx_executions_source_event_id ON executions(source_event_id);
CREATE INDEX idx_executions_jira_issue_key ON executions(jira_issue_key);
CREATE INDEX idx_executions_conversation_id ON executions(conversation_id);
CREATE INDEX idx_executions_created_at ON executions(created_at);
```

#### `github_pull_requests`

```sql
CREATE TABLE github_pull_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pr_number INTEGER NOT NULL,
    repository VARCHAR(255) NOT NULL,
    owner VARCHAR(255) NOT NULL,
    branch VARCHAR(255),
    title TEXT,
    state VARCHAR(20) NOT NULL DEFAULT 'open',
    execution_id UUID REFERENCES executions(id),
    pr_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(pr_number, repository)
);

CREATE INDEX idx_github_pr_pr_number ON github_pull_requests(pr_number);
CREATE INDEX idx_github_pr_repository ON github_pull_requests(repository);
```

#### `review_iterations`

```sql
CREATE TABLE review_iterations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    execution_id UUID NOT NULL REFERENCES executions(id),
    iteration_number INTEGER NOT NULL,
    review_comment_id BIGINT,
    reviewer VARCHAR(255),
    comment_body TEXT,
    pr_number INTEGER,
    repository VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_review_iterations_execution ON review_iterations(execution_id);
CREATE INDEX idx_review_iterations_pr ON review_iterations(pr_number, repository);
```

#### `execution_events`

```sql
CREATE TABLE execution_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    execution_id VARCHAR(255) NOT NULL REFERENCES executions(execution_id),
    event_type VARCHAR(50) NOT NULL,
    event_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_execution_events_execution_id ON execution_events(execution_id);
CREATE INDEX idx_execution_events_created_at ON execution_events(created_at);
```

### 3.3 Query Patterns

| Query | Table | Index Used |
|-------|-------|------------|
| Find execution by correlation ID | `executions` | `idx_executions_execution_id` |
| Find execution by webhook event ID (idempotency) | `executions` | `idx_executions_source_event_id` |
| List executions for a Jira issue | `executions` | `idx_executions_jira_issue_key` |
| Find conversation for execution | `executions` | `idx_executions_conversation_id` |
| List pending executions | `executions` | `idx_executions_state` |
| List review iterations for a PR | `review_iterations` | `idx_review_iterations_pr` |
| Count executions by repository | `executions` | `idx_executions_state` + `repository` |

---

## 4. Execution Lifecycle

### 4.1 State Machine

```
                  ┌─────────────┐
                  │  RECEIVED   │
                  └──────┬──────┘
                         │
                         ▼
                  ┌─────────────┐
           ┌─────►│   QUEUED    │◄────────┐
           │      └──────┬──────┘         │
           │             │                │
           │             ▼                │
           │      ┌─────────────┐         │
           │      │   RUNNING   │         │
           │      └──────┬──────┘         │
           │             │                │
           │    ┌────────┼────────┐       │
           │    │        │        │       │
           ▼    ▼        ▼        ▼       │
     ┌──────────┐ ┌──────────┐ ┌──────────┐
     │COMPLETED │ │  FAILED  │ │CANCELLED │
     └──────────┘ └──────────┘ └──────────┘
```

### 4.2 State Transitions

| Current State | Event | Next State | Notes |
|--------------|-------|-----------|-------|
| - | Webhook received | RECEIVED | Initial state after idempotency check |
| RECEIVED | Queued for processing | QUEUED | After webhook returns 202 |
| QUEUED | Processing started | RUNNING | Background task begins execution |
| RUNNING | Execution succeeded | COMPLETED | Agent finished successfully |
| RUNNING | Execution failed | FAILED | Agent or system error |
| RUNNING | Execution cancelled | CANCELLED | User or system cancellation |
| QUEUED | Duplicate or skip | CANCELLED | Idempotency match |

### 4.3 Execution Record

```python
@dataclass
class ExecutionRecord:
    execution_id: str          # UUID, correlation ID
    source_type: str           # 'jira' | 'github'
    source_event_id: str       # Webhook event ID for idempotency
    state: ExecutionState      # Current state
    jira_issue_key: str | None
    github_pr_number: int | None
    repository: str | None
    branch: str | None
    conversation_id: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
```

### 4.4 ExecutionStore

```python
@dataclass
class ExecutionStore:
    """Manages execution records in the database."""

    async def create_execution(
        self,
        source_type: str,
        source_event_id: str,
        jira_issue_key: str | None = None,
        github_pr_number: int | None = None,
        repository: str | None = None,
        branch: str | None = None,
    ) -> ExecutionRecord: ...

    async def update_state(
        self,
        execution_id: str,
        state: ExecutionState,
        error_message: str | None = None,
        conversation_id: str | None = None,
    ) -> ExecutionRecord: ...

    async def get_execution(
        self, execution_id: str
    ) -> ExecutionRecord | None: ...

    async def get_execution_by_source_event(
        self, source_event_id: str
    ) -> ExecutionRecord | None: ...

    async def list_executions(
        self,
        source_type: str | None = None,
        state: ExecutionState | None = None,
        jira_issue_key: str | None = None,
        github_pr_number: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionRecord]: ...

    async def count_executions(
        self,
        source_type: str | None = None,
        state: ExecutionState | None = None,
        jira_issue_key: str | None = None,
        repository: str | None = None,
    ) -> int: ...
```

---

## 5. API Design

### 5.1 Jira Webhook Endpoint

```
POST /api/v1/jira/webhook

Headers:
  Content-Type: application/json
  x-hub-signature: <HMAC-SHA256 signature>

Request Body (Jira webhook payload):
{
  "webhookEvent": "jira:issue_created",
  "issue": {
    "id": "10001",
    "key": "KAN-17",
    "fields": {
      "summary": "Feature title",
      "description": "Description",
      "issuetype": {"name": "Story"},
      "priority": {"name": "Medium"},
      "reporter": {"displayName": "User", "emailAddress": "user@example.com"},
      "labels": ["automation"]
    }
  }
}

Response (202 Accepted):
{
  "execution_id": "exec_xxxxx",
  "status": "RECEIVED",
  "message": "Execution request received"
}

Response (409 Conflict - Duplicate):
{
  "execution_id": "exec_xxxxx",
  "status": "DUPLICATE",
  "message": "Webhook event already processed"
}
```

### 5.2 GitHub Webhook Endpoint

```
POST /api/v1/github/webhook

Headers:
  Content-Type: application/json
  X-Hub-Signature-256: <HMAC-SHA256 signature>
  X-GitHub-Event: pull_request_review_comment

Request Body (GitHub webhook payload):
{
  "action": "created",
  "comment": {
    "id": 123456789,
    "body": "Please fix this bug...",
    "user": {"login": "reviewer"},
    "pull_request_review_id": 987654321
  },
  "pull_request": {
    "number": 42,
    "head": {"ref": "feature-branch", "sha": "abc123"},
    "base": {"ref": "main", "sha": "def456"},
    "title": "PR Title",
    "body": "PR Description"
  },
  "repository": {
    "full_name": "owner/repo",
    "owner": {"login": "owner"},
    "name": "repo"
  },
  "installation": {"id": 12345}
}

Response (202 Accepted):
{
  "execution_id": "exec_xxxxx",
  "status": "RECEIVED",
  "message": "Review comment received, execution queued"
}

Response (409 Conflict - Duplicate):
{
  "execution_id": "exec_xxxxx",
  "status": "DUPLICATE",
  "message": "Review comment already processed"
}
```

---

## 6. OpenHands Integration Plan

### 6.1 Conversation Creation

The existing `start_app_conversation()` flow will be reused:

```python
async def _create_openhands_conversation(
    self,
    execution_id: str,
    prompt: str,
    repository: str,
    branch: str,
    pr_number: int | None = None,
    jira_issue_key: str | None = None,
) -> AppConversationStartTask:
    """Create and start an OpenHands conversation for an execution."""
    request = AppConversationStartRequest(
        initial_message=SendMessageRequest(
            message=prompt,
            role='user',
        ),
        selected_repository=repository,
        selected_branch=branch,
        pr_number=[pr_number] if pr_number else [],
        trigger=ConversationTrigger.AUTOMATION,
        title=f'{source_type}: {issue_key or pr_number}',
    )
    async_gen = app_conversation_service.start_app_conversation(request)
    result = await anext(async_gen)
    asyncio.create_task(_consume_remaining(async_gen, ...))
    return result
```

### 6.2 Branch Creation

Branch creation will use the existing GitHub API integration (`GithubMixinBase`):

```python
async def create_branch(
    self,
    repository: str,
    base_branch: str,
    new_branch: str,
    installation_id: int,
) -> str:
    """Create a branch from base_branch."""
    token = await self._get_installation_token(installation_id)
    # Use GitHub API: POST /repos/{owner}/{repo}/git/refs
    ...
```

### 6.3 Commit and PR Operations

Reuse existing infrastructure:
- `openhands/app_server/integrations/github/service/prs.py` — PR operations
- `openhands/app_server/integrations/github/service/repos.py` — repo operations

### 6.4 Integration Points

| Operation | Existing Code | Reuse Strategy |
|-----------|--------------|----------------|
| Conversation creation | `AppConversationService.start_app_conversation()` | Direct reuse |
| Git operations | `git_router.py`, GitHub service mixins | Direct reuse |
| Token management | `TokenManager` | Direct reuse |
| GitHub auth | `GithubIntegration` with `AppAuth` | Direct reuse |
| Jira API calls | `JiraManager` HTTP client | Create new client for stand-alone use |
| Event callbacks | `EventCallbackProcessor` | Extend for execution completion |

### 6.5 Risks

| Risk | Mitigation |
|------|------------|
| Existing integration managers tightly coupled to OAuth/user model | Build new automation services as separate modules, not extending existing managers |
| No existing queue for async execution | Use FastAPI `BackgroundTasks` as existing patterns do; add DB-based queue if throughput demands it |
| GitHub API rate limits | Track rate limits in execution records; implement backoff |
| Long-running agent executions | Set appropriate timeouts; persist execution state for resumability |

---

## 7. Langfuse Observability Design

### 7.1 Current State

Langfuse is currently configured **externally** via the LiteLLM proxy layer. The codebase passes LLM metadata to LiteLLM, which handles Langfuse tracing internally. There is no direct Langfuse SDK integration in the OpenHands codebase.

### 7.2 Proposed Trace Hierarchy

```
Trace: exec_{execution_id}
├── Span: webhook_ingestion
│   ├── event: signature_validation
│   ├── event: idempotency_check
│   └── event: execution_record_created
├── Span: openhands_execution
│   ├── Span: conversation_creation
│   ├── Span: agent_execution
│   │   ├── Generation: LLM call 1 (model, prompt, completion, tokens, cost)
│   │   ├── Generation: LLM call 2
│   │   └── ...
│   ├── Span: git_operations
│   │   ├── Event: branch_creation
│   │   ├── Event: commit
│   │   └── Event: pr_creation
│   └── Span: tool_calls
│       ├── Event: bash_command
│       ├── Event: file_edit
│       └── Event: web_search
└── Span: post_execution
    ├── Event: status_update
    └── Event: callback_notification

Tags:
  execution_id: str
  conversation_id: str
  jira_issue_key: str | None
  repository: str
  branch: str
  pr_number: int | None
  source_type: str
  model: str
  final_status: str
```

### 7.3 Implementation Approach

Rather than integrating Langfuse SDK directly (which would be a large change), we'll:

1. **Set `trace_id` on LiteLLM completions**: Pass `execution_id` as the Langfuse trace ID via LiteLLM metadata
2. **Structured logging**: All log entries include correlation fields
3. **Execution events table**: Persist all state transitions for query-based analytics
4. **Post-execution analytics**: After completion, emit an execution analytics event with full cost/token data

### 7.4 LiteLLM Metadata Integration

```python
llm_metadata = {
    'trace_id': f'exec_{execution_id}',
    'tags': {
        'execution_id': execution_id,
        'conversation_id': conversation_id,
        'jira_issue_key': jira_issue_key,
        'repository': repository,
        'branch': branch,
        'pr_number': pr_number,
        'source_type': source_type,
    }
}
```

---

## 8. Correlation & Traceability

### 8.1 Correlation ID Format

```
exec_{uuid_short}
```

Example: `exec_a1b2c3d4e5f6`

### 8.2 Propagation Points

| Layer | Where | How |
|-------|-------|-----|
| Webhook ingestion | `execution_id` generated | Stored in `execution.execution_id` |
| Structured logs | Every log entry | `extra={'execution_id': ..., 'conversation_id': ...}` |
| OpenHands conversation | Conversation title/metadata | `title` field, `trigger=AUTOMATION` |
| GitHub API calls | Branch name, commit messages | Branch: `feature/KAN-123-exec_xxx` |
| Jira API calls | Comment text | Includes `execution_id` in comment |
| Langfuse traces | Trace ID | `exec_{execution_id}` |
| Database | Primary correlation key | `executions.execution_id` unique index |

### 8.3 Structured Logging Configuration

```python
LOG_CONFIG = {
    'execution_id': execution_id,
    'conversation_id': conversation_id,
    'repository': repository,
    'branch': branch,
    'jira_issue_key': jira_issue_key,
    'pr_number': pr_number,
}
```

---

## 9. Idempotency Strategy

### 9.1 Webhook Event ID Extraction

**Jira**: Use `webhookEvent` + `issue.id` + timestamp window
```python
def get_jira_event_id(payload: dict) -> str:
    return f"jira:{payload['webhookEvent']}:{payload['issue']['id']}:{payload['issue']['fields']['updated']}"
```

**GitHub**: Use `X-GitHub-Delivery` header or `comment.id`
```python
def get_github_event_id(payload: dict, delivery_id: str) -> str:
    return f"github:{delivery_id}:{payload.get('comment', {}).get('id', '')}"
```

### 9.2 Duplicate Detection Flow

```
Webhook received
    │
    ▼
Compute event_id from payload
    │
    ▼
Query executions.source_event_id
    │
    ├── Found → Return 409 Conflict (duplicate)
    │
    └── Not found → Create execution (state=RECEIVED)
                       │
                       ▼
                    Continue processing
```

### 9.3 Database Enforcement

```sql
CREATE UNIQUE INDEX idx_executions_source_event_id
    ON executions(source_event_id)
    WHERE source_event_id IS NOT NULL;
```

---

## 10. Branch Strategy

### 10.1 Naming Convention

| Source | Pattern | Example |
|--------|---------|---------|
| Jira issue | `{issue_type}/{ISSUE-KEY}-{slug}` | `feature/KAN-17-automation-platform` |
| Jira bugfix | `bugfix/{ISSUE-KEY}-{slug}` | `bugfix/KAN-17-fix-npe` |
| GitHub review | `review/{PR-BRANCH}-iteration-{N}` | `review/feature-branch-iteration-2` |

### 10.2 Slug Generation

```python
def generate_slug(text: str, max_length: int = 40) -> str:
    """Generate a URL-safe slug from text."""
    slug = re.sub(r'[^a-zA-Z0-9\s-]', '', text.lower())
    slug = re.sub(r'[\s-]+', '-', slug).strip('-')
    return slug[:max_length].rstrip('-')
```

### 10.3 Deterministic Branch Names

Branches are deterministic and traceable:
- Jira: `{issue_type}/{ISSUE-KEY}-{slugify(summary)}`
- GitHub: `review/{original_branch}-iteration-{iteration_count}`

### 10.4 Branch Lifecycle

```
Jira Workflow:
  1. Create branch from base (main/develop)
  2. Agent works on branch
  3. Agent commits changes
  4. PR created from branch → base
  5. Branch persists (linked to PR)

GitHub Review Workflow:
  1. Branch already exists (from original PR)
  2. Agent fetches existing branch
  3. Agent commits new changes on existing branch
  4. PR automatically updated (pushed to same branch)
```

---

## 11. Failure Handling

### 11.1 Failure Scenarios

| Scenario | Detection | Recovery | Retry |
|----------|-----------|----------|-------|
| Invalid webhook signature | 403 in webhook | Log and reject | No retry |
| Duplicate webhook | Idempotency check | Return 409 | No retry |
| Jira API failure | HTTP error from Jira | Log, mark FAILED, post error | 3 attempts with backoff |
| GitHub API failure | HTTP error from GitHub | Log, mark FAILED, post error | 3 attempts with backoff |
| Branch creation failure | Git API error | Log, mark FAILED | 2 attempts |
| Commit failure | Git API error | Log, mark FAILED | 2 attempts |
| PR creation failure | GitHub API error | Log, mark FAILED | 2 attempts |
| OpenHands conversation failure | Agent error / timeout | Log, mark FAILED, post error | No retry (manual) |
| LLM authentication error | LiteLLM error | Log, mark FAILED | No retry |
| Session expired | Agent server error | Log, mark FAILED | No retry |

### 11.2 Error Persistence

```python
async def handle_execution_error(
    execution_id: str,
    error_message: str,
    error_type: str,
) -> None:
    """Handle execution failure - persist error and update state."""
    await execution_store.update_state(
        execution_id=execution_id,
        state=ExecutionState.FAILED,
        error_message=error_message,
    )
    await execution_events_store.create_event(
        execution_id=execution_id,
        event_type='error',
        event_data={
            'error_type': error_type,
            'error_message': error_message,
            'timestamp': datetime.utcnow().isoformat(),
        },
    )
```

### 11.3 Retry Strategy

```python
RETRY_CONFIG = {
    'jira_api': {'max_retries': 3, 'backoff': 'exponential', 'base_delay': 1.0},
    'github_api': {'max_retries': 3, 'backoff': 'exponential', 'base_delay': 1.0},
    'branch_creation': {'max_retries': 2, 'backoff': 'linear', 'delay': 5.0},
    'commit': {'max_retries': 2, 'backoff': 'linear', 'delay': 5.0},
    'pr_creation': {'max_retries': 2, 'backoff': 'linear', 'delay': 5.0},
}
```

### 11.4 Langfuse Trace Preservation

Even on failure:
- The execution trace is created before execution starts
- State transitions are logged as events within the trace
- On failure, an error event is added to the trace
- The trace is finalized with `status=ERROR` and the error description

---

## 12. Implementation Plan

### Phase 1: Foundation (Database & Models)

1. Create Alembic migration for new tables:
   - `executions`, `jira_issues`, `github_pull_requests`, `review_iterations`, `execution_events`
2. Create SQLAlchemy model files in `enterprise/storage/`
3. Create `ExecutionStore` dataclass
4. Create `ExecutionRecord`, `ExecutionState` data models

### Phase 2: Execution Service

1. Create `ExecutionService` with state machine logic
2. Implement idempotency checking
3. Implement execution state transitions
4. Add structured logging with correlation IDs

### Phase 3: Jira Automation

1. Create `JiraAutomationService` (standalone, not extending `JiraManager`)
2. Create `POST /api/v1/jira/webhook` router in enterprise routes
3. Implement webhook signature validation for Jira
4. Implement Jira payload parsing
5. Integrate with `ExecutionService`
6. Create OpenHands conversation for Jira issues
7. Implement branch creation and PR creation via GitHub API
8. Create automation callback processor for posting results to Jira

### Phase 4: GitHub Automation

1. Create `GitHubAutomationService` (standalone)
2. Create `POST /api/v1/github/webhook` router in enterprise routes
3. Implement GitHub webhook signature validation
4. Implement PR review comment payload parsing
5. Implement PR context and review fetching
6. Integrate with `ExecutionService`
7. Create NEW OpenHands conversation for each review cycle
8. Implement commit to existing branch and PR update

### Phase 5: Observability

1. Implement structured logging with all correlation fields
2. Integrate execution_id into LiteLLM metadata for Langfuse
3. Create execution analytics events
4. Implement post-execution cost/token collection

### Phase 6: Testing

1. Unit tests for `ExecutionStore`
2. Unit tests for `ExecutionService` state machine
3. Unit tests for Jira webhook validation and payload parsing
4. Unit tests for GitHub webhook validation and payload parsing
5. Unit tests for branch naming and slug generation
6. Integration tests for webhook endpoints
7. Integration tests for end-to-end execution flow

---

## File Structure

The new files will be organized as follows:

```
enterprise/
├── integrations/
│   ├── automation/
│   │   ├── __init__.py
│   │   ├── execution_service.py      # ExecutionService - state machine
│   │   ├── execution_store.py        # ExecutionStore - DB operations
│   │   ├── execution_models.py        # Data models & enums
│   │   ├── jira_automation_service.py # Jira webhook → execution
│   │   ├── github_automation_service.py # GitHub webhook → execution
│   │   ├── openhands_client.py       # OpenHands conversation helper
│   │   └── correlation.py            # Correlation ID utilities
│   ├── automation_jira/
│   │   ├── __init__.py
│   │   ├── jira_webhook_handler.py   # Jira payload parsing
│   │   ├── jira_client.py            # Jira REST API client
│   │   └── jira_callback.py          # Post-execution Jira callback
│   ├── automation_github/
│   │   ├── __init__.py
│   │   ├── github_webhook_handler.py # GitHub payload parsing
│   │   ├── github_client.py          # GitHub API client
│   │   └── github_callback.py        # Post-execution GitHub callback
│   └── templates/
│       └── automation/
│           ├── jira_new_conversation.j2
│           └── github_review_conversation.j2
├── server/
│   └── routes/
│       └── automation/
│           ├── __init__.py
│           ├── jira_webhook_router.py    # POST /api/v1/jira/webhook
│           └── github_webhook_router.py  # POST /api/v1/github/webhook
├── storage/
│   ├── execution.py                  # SQLAlchemy model
│   ├── jira_issue.py                 # SQLAlchemy model
│   ├── github_pull_request.py        # SQLAlchemy model
│   ├── review_iteration.py           # SQLAlchemy model
│   └── execution_event.py            # SQLAlchemy model
├── migrations/
│   └── versions/
│       └── 120.py                    # Automation tables migration
└── tests/
    └── unit/
        └── automation/
            ├── test_execution_service.py
            ├── test_jira_automation_service.py
            ├── test_github_automation_service.py
            ├── test_execution_store.py
            └── test_webhook_routers.py
```

---

## 13. Repository Resolution Design

### 13.1 Problem

Jira webhook payloads contain a **project key** (e.g., `"KAN"`) but **never** contain a GitHub repository identifier. The automation platform needs to know which repository to clone, branch, and create PRs against.

### 13.2 Resolution Strategy (Cascading)

The `JiraProjectRepositoryResolver` resolves the target GitHub repository using a cascading lookup:

```
1. Jira Custom Field (per-issue override)
   ↓ (if not present or invalid format)
2. Project → Repository Mapping Table (DB-backed)
   ↓ (if no mapping exists)
3. ❌ Fail with RepositoryNotResolvedError
```

**No silent defaults.** If no mapping can be found, the webhook response returns a clear error.

### 13.3 Layer 1: Jira Custom Field (Issue-Level Override)

Each project mapping can optionally specify a `custom_field_id` (e.g., `customfield_12345`). When configured:

1. The resolver checks if the Jira issue's `fields` dict contains that custom field key
2. If present and in `"owner/repo-name"` format, it's used as the target repository
3. Supports string values, `{"value": "..."}` objects, and `{"name": "..."}` objects

### 13.4 Layer 2: Database Mapping Table

Table: `jira_project_repositories`

| Column | Type | Description | Example |
|--------|------|-------------|---------|
| `jira_project_key` | VARCHAR(50) UNIQUE | Jira project identifier | `KAN` |
| `repository` | VARCHAR(255) | GitHub repo full name | `thatIsSharif/openhands` |
| `owner` | VARCHAR(255) | Repository owner | `thatIsSharif` |
| `default_branch` | VARCHAR(50) | Branch to base PRs on | `main` |
| `custom_field_id` | VARCHAR(50) NULL | Jira custom field ID | `customfield_12345` |

### 13.5 Layer 3: Fail with Clear Error

When no mapping exists, the resolver raises `RepositoryNotResolvedError` and the webhook returns `{"status": "failed", "error": "No repository mapping for Jira project ..."}`.

### 13.6 Admin API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/admin/jira-project-repos` | Create/update mapping |
| GET | `/api/v1/admin/jira-project-repos` | List all mappings |
| GET | `/api/v1/admin/jira-project-repos/{project_key}` | Get single mapping |
| DELETE | `/api/v1/admin/jira-project-repos/{project_key}` | Delete mapping |

### 13.7 Future Multi-Repository

Current schema: one repo per Jira project. For future multi-repo support, add a `labels_filter TEXT[]` column and allow multiple rows per project key. The resolver would match based on issue labels.
