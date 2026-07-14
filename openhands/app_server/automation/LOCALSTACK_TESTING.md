# Testing Automation Archive/Restore with LocalStack

This guide covers end-to-end testing of the archive-to-S3 flow using
[LocalStack](https://docs.localstack.cloud/getting-started/) as the S3
backend. No AWS account is required.

## Architecture (quick recap)

```
Agent finishes → callback processor archives conversation → S3
→ sandbox destroyed → later webhook arrives → new sandbox
→ archive restored from S3 → conversation resumes
```

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Docker | 24+ | `docker` |
| Docker Compose | v2 | `docker compose` |
| `awscli-local` | latest | `pip install awscli-local` |
| `jq` | any | `apt install jq` / `brew install jq` |

### Install `awslocal`

```bash
pip install awscli-local
```

This wraps the AWS CLI and auto-routes requests to `http://localhost:4566`.

## Step 1 — Start LocalStack

```bash
docker run --rm -d \
  --name localstack \
  -p 4566:4566 \
  -p 4510-4559:4510-4559 \
  localstack/localstack:latest
```

Wait for it to be ready:

```bash
until awslocal s3 ls 2>/dev/null; do sleep 1; done
echo "LocalStack ready"
```

## Step 2 — Create the S3 bucket

```bash
awslocal s3 mb s3://openhands-automation-archive
```

Verify:

```bash
awslocal s3 ls
# Expected: 2025-... openhands-automation-archive
```

## Step 3 — Configure the environment

Create a `.env` file or export these variables before starting OpenHands:

```bash
# ── S3 persistence ────────────────────────────
export USE_AWS_S3=false                          # LocalStack mode
export LOCALSTACK_ENDPOINT=http://localhost:4566 # must match step 1
export AWS_S3_BUCKET=openhands-automation-archive
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test

# ── Automation (required) ─────────────────────
export JIRA_DOMAIN=your-domain.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_KEY=your-jira-api-token
```

> **Why `AWS_ACCESS_KEY_ID=test`?** LocalStack accepts any non-empty
> credentials in test mode. The `create_s3_client()` factory defaults
> to `test`/`test` when `USE_AWS_S3=false` and no credentials are set.
> You can also use real-looking values — LocalStack won't validate them
> unless `ENFORCE_IAM=1` is set on the LocalStack container.

## Step 4 — Start OpenHands

```bash
docker compose up --build
```

Or with explicit env:

```bash
USE_AWS_S3=false \
LOCALSTACK_ENDPOINT=http://localhost:4566 \
AWS_S3_BUCKET=openhands-automation-archive \
AWS_ACCESS_KEY_ID=test \
AWS_SECRET_ACCESS_KEY=test \
docker compose up --build
```

Wait for the app to be ready at `http://localhost:3000`.

## Step 5 — Trigger an automation

### Jira flow

1. Assign a Jira issue to the configured target account.
2. The Jira webhook triggers `POST /api/v1/webhooks/jira/start`.
3. A new conversation starts in a fresh sandbox.
4. When the agent finishes:
   - The callback processor downloads the conversation trajectory zip.
   - It uploads `archives/jira/{ISSUE-KEY}/{execution_id}.tar.gz` to S3.
   - The sandbox is destroyed.

### GitHub flow

1. Submit a PR review (approved or changes_requested).
2. GitHub webhook triggers `POST /api/v1/webhooks/git/github/webhook`.
3. Same archive-on-completion behavior as Jira.

## Step 6 — Verify the archive was created

List objects in the bucket:

```bash
awslocal s3 ls s3://openhands-automation-archive/archives/ --recursive
```

Example output:

```
2025-07-14 12:00:00   1234567 archives/jira/PROJ-123/exec_abc123.tar.gz
2025-07-14 12:05:00    987654 archives/github/owner/repo/pr-42/exec_def456.tar.gz
```

## Step 7 — Inspect an archive

Download and inspect what's inside:

```bash
# Download
awslocal s3 cp \
  s3://openhands-automation-archive/archives/jira/PROJ-123/exec_abc123.tar.gz \
  /tmp/archive.tar.gz

# List contents
tar tzf /tmp/archive.tar.gz
```

Expected structure:

```
conversation.zip          # Full trajectory from /file/download-trajectory
archive-meta.txt          # execution_id, conversation_id, mapping_key
```

Inspect the conversation zip:

```bash
tar xzf /tmp/archive.tar.gz -C /tmp/archive
unzip -l /tmp/archive/conversation.zip
```

You should see:

```
base_state.json          # Agent state serialized by SDK ConversationState
meta.json                # Conversation metadata
events/                  # Directory of individual event JSON files
```

## Step 8 — Test restore (trigger a follow-up event)

### Jira: @openhands mention

1. Add a comment to the same Jira issue containing `@openhands`.
2. The webhook router finds the conversation → sandbox is MISSING (was destroyed).
3. It looks up `get_latest_archived_execution(jira_issue_key=...)`.
4. Creates a fresh sandbox, downloads the archive from S3, extracts it.
5. Calls `POST /api/conversations` with the same conversation_id → SDK resume path.
6. The new comment is forwarded as a user message.

### GitHub: new review on same PR

Same flow as Jira but keyed on `pr_number + repository`.

### Verify via logs

Watch the logs during a restore:

```bash
docker compose logs -f openhands | grep -E '\[SandboxArchive\]|\[Automation\]'
```

Expected log sequence:

```
[Automation] Sandbox sand_XYZ for PROJ-123 not available, attempting archive restore
[Automation] Found archived execution exec_abc123 for PROJ-123 at archives/jira/PROJ-123/exec_abc123.tar.gz
[SandboxArchive] Restored conversation abc-def-123 from archives/jira/PROJ-123/exec_abc123.tar.gz
[Automation] Restored and resumed conversation abc-def-123 from archive ...
```

## Step 9 — Verify the conversation continued

1. Open OpenHands UI at `http://localhost:3000`.
2. Find the conversation by title or Jira issue key.
3. Confirm the full chat history is present (events before archive + new messages).
4. The agent should respond to the new comment with full context of the previous work.

## Troubleshooting

### "No archived execution found"

The execution hasn't reached `ARCHIVED` state yet. Check:

```bash
# In OpenHands DB (adjust connection as needed)
sqlite3 ~/.openhands/openhands.db \
  "SELECT execution_id, state, archive_location FROM executions WHERE jira_issue_key='PROJ-123' ORDER BY updated_at DESC LIMIT 3"
```

Expected: last row should have `state=ARCHIVED` and a non-null `archive_location`.

### "Failed to download trajectory"

The sandbox was already destroyed before the callback ran. Check the callback
processor logs for timing issues. This is rare but can happen if the sandbox
is destroyed externally (e.g. by a timeout).

### LocalStack connection refused

```bash
# Confirm LocalStack is running
docker ps -f name=localstack

# Check the endpoint
awslocal s3 ls
```

If `awslocal` hangs, try explicitly:

```bash
aws --endpoint-url=http://localhost:4566 s3 ls
```

### Archive is empty or corrupted

Check the agent server is reachable from the OpenHands backend container:

```bash
docker compose exec openhands curl -s http://host.docker.internal:4566/_localstack/health | jq
```

All services should report `"available"` or `"running"`.

## Cleanup

```bash
docker compose down
docker rm -f localstack
```

## Switching to real AWS S3

When ready to move from LocalStack to real AWS:

```bash
export USE_AWS_S3=true
export AWS_S3_BUCKET=my-production-bucket
export AWS_REGION=us-east-1
# Credentials via standard boto3 chain:
#   AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY, or
#   AWS_PROFILE, or IAM instance role
```

The `LOCALSTACK_ENDPOINT` variable is ignored when `USE_AWS_S3=true`.
