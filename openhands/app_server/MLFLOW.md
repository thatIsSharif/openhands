# MLflow Integration with OpenHands

This document describes the MLflow integration for OpenHands conversation observability.
MLflow captures token usage, cost, latency, and conversation metadata as MLflow runs,
enabling analysis through MLflow's built-in dashboards and comparison tools.

> **Note:** This integration is for the OpenHands community (OSS) version only.
> It does not modify any enterprise or paid version code.

---

## Architecture

The MLflow integration uses a lightweight, thread-safe tracker service that hooks into
the existing webhook-based conversation lifecycle:

```
OpenHands Agent Server (sandbox)
  │
  ├─ POST /webhooks/conversations  →  on_conversation_update()
  │                                    └── Start MLflow run (on new conversation)
  │
  ├─ POST /webhooks/events/{id}    →  on_event()
  │                                    ├── Process stats → log_metrics()
  │                                    └── Terminal state → end_conversation()
  │
  └─ _track_conversation_terminal()
       └── Log final metrics + end MLflow run
```

### Key Components

| Component | File | Purpose |
|---|---|---|
| `MLflowTracker` | `openhands/app_server/services/mlflow_tracker.py` | Thread-safe MLflow run management |
| `get_mlflow_tracker()` | Same file | Singleton accessor |
| `is_mlflow_enabled()` | Same file | Check if MLflow is configured |
| Integration | `openhands/app_server/event_callback/webhook_router.py` | Lifecycle hooks |

### Metrics Captured

| Metric | Source | Description |
|---|---|---|
| `prompt_tokens` | `ConversationStats` → `TokenUsage` | Input tokens used |
| `completion_tokens` | `ConversationStats` → `TokenUsage` | Output tokens generated |
| `cache_read_tokens` | `ConversationStats` → `TokenUsage` | Prompt cache reads |
| `cache_write_tokens` | `ConversationStats` → `TokenUsage` | Prompt cache writes |
| `reasoning_tokens` | `ConversationStats` → `TokenUsage` | Reasoning tokens (thinking models) |
| `accumulated_cost` | `ConversationStats` → `Metrics` | Total LLM cost in USD |
| `latency_seconds` | Via step tracking | Per-turn response time |

### Parameters Logged

| Parameter | Description |
|---|---|
| `conversation_id` | Unique conversation identifier |
| `repository` | Git repository (e.g., `owner/repo`) |
| `branch` | Git branch |
| `trigger` | Source: `gui`, `automation`, `resolver`, etc. |
| `llm_model` | LLM model name (e.g., `gpt-4`, `claude-3`) |
| `title` | Conversation title |
| `start_time` | ISO 8601 timestamp |
| `end_time` | ISO 8601 timestamp |
| `status` | Final status: `finished`, `error`, `stopped` |
| `duration_seconds` | Wall-clock duration |
| `total_turns` | Number of agent turns |

### Artifacts Stored

| Artifact | Condition | Description |
|---|---|---|
| `error.txt` | On error | Error message if conversation failed |
| `conversation_summary.txt` | On completion | Conversation summary (future) |

---

## Setup

### Prerequisites

- Python 3.9+
- `mlflow` package installed
- OpenHands app server running

### 1. Install MLflow

```bash
pip install mlflow
```

### 2. Start MLflow Server

**Option A: Local (SQLite)**

```bash
mkdir -p mlflow_data
mlflow server \
  --backend-store-uri sqlite:///mlflow_data/mlflow.db \
  --default-artifact-root ./mlflow_data/artifacts \
  --host 0.0.0.0 \
  --port 5000
```

**Option B: Docker**

```bash
docker run -d --name mlflow-server \
  -p 5000:5000 \
  -v $(pwd)/mlflow_data:/mlflow \
  ghcr.io/mlflow/mlflow:latest \
  mlflow server \
    --backend-store-uri sqlite:///mlflow/mlflow.db \
    --default-artifact-root /mlflow/artifacts \
    --host 0.0.0.0
```

**Option C: PostgreSQL (production)**

```bash
mlflow server \
  --backend-store-uri postgresql://user:pass@localhost/mlflow \
  --default-artifact-root s3://my-bucket/mlflow-artifacts \
  --host 0.0.0.0 \
  --port 5000
```

### 3. Configure OpenHands

Set environment variables:

```bash
# Required: MLflow server URL
export MLFLOW_TRACKING_URI=http://localhost:5000

# Optional: Experiment name (default: openhands-conversations)
export MLFLOW_EXPERIMENT_NAME=openhands-conversations
```

---

## Running with Docker Compose

Add MLflow to your `docker-compose.yml`:

```yaml
version: '3.8'
services:
  mlflow:
    image: ghcr.io/mlflow/mlflow:latest
    ports:
      - "5000:5000"
    volumes:
      - mlflow_data:/mlflow
    command: >
      mlflow server
      --backend-store-uri sqlite:///mlflow/mlflow.db
      --default-artifact-root /mlflow/artifacts
      --host 0.0.0.0

  openhands-app:
    # ... existing OpenHands config ...
    environment:
      - MLFLOW_TRACKING_URI=http://mlflow:5000
      - MLFLOW_EXPERIMENT_NAME=openhands-conversations
    depends_on:
      - mlflow

volumes:
  mlflow_data:
```

---

## Verification

### 1. Check MLflow is Accessible

```bash
curl http://localhost:5000/api/2.0/mlflow/experiments/list
```

Expected response:
```json
{"experiments": [{"experiment_id": "0", "name": "Default"}]}
```

### 2. Create a Test Conversation

Run an OpenHands conversation (either via GUI or automation). After the conversation completes,
you should see an MLflow run appear.

### 3. Verify Metrics Were Captured

```bash
# List runs in the experiment
curl "http://localhost:5000/api/2.0/mlflow/runs/search?experiment_ids=[1]"
```

Or check metrics directly:
```bash
# Replace RUN_ID with the actual run ID
curl "http://localhost:5000/api/2.0/mlflow/metrics/get-history?run_id=RUN_ID&metric_key=prompt_tokens"
```

### 4. Open MLflow UI

Navigate to `http://localhost:5000` in your browser. Select the experiment
(`openhands-conversations`) to view runs.

---

## Example MLflow Queries

### List All Runs with Metrics

```python
import mlflow

client = mlflow.MlflowClient()
experiment = client.get_experiment_by_name("openhands-conversations")
runs = client.search_runs(
    experiment_ids=[experiment.experiment_id],
    order_by=["attributes.start_time DESC"],
    max_results=10,
)

for run in runs:
    print(f"Run: {run.data.tags.get('mlflow.runName', 'unknown')}")
    print(f"  Model: {run.data.params.get('llm_model', 'N/A')}")
    print(f"  Cost: ${run.data.metrics.get('accumulated_cost', 0):.4f}")
    print(f"  Tokens: {run.data.metrics.get('prompt_tokens', 0)} in, "
          f"{run.data.metrics.get('completion_tokens', 0)} out")
```

### Cost Analysis by Trigger Type

```python
import mlflow

client = mlflow.MlflowClient()
experiment = client.get_experiment_by_name("openhands-conversations")
runs = client.search_runs(
    experiment_ids=[experiment.experiment_id],
    order_by=["attributes.start_time DESC"],
)

by_trigger = {}
for run in runs:
    trigger = run.data.params.get("trigger", "unknown")
    cost = run.data.metrics.get("accumulated_cost", 0)
    by_trigger[trigger] = by_trigger.get(trigger, 0) + cost

print("Cost by Trigger:")
for trigger, cost in sorted(by_trigger.items(), key=lambda x: -x[1]):
    print(f"  {trigger}: ${cost:.4f}")
```

### Token Usage Over Time

```python
import mlflow

client = mlflow.MlflowClient()
experiment = client.get_experiment_by_name("openhands-conversations")
runs = client.search_runs(
    experiment_ids=[experiment.experiment_id],
    order_by=["attributes.start_time ASC"],
)

dates = []
total_tokens = []
for run in runs:
    dates.append(run.info.start_time)
    pt = run.data.metrics.get("prompt_tokens", 0)
    ct = run.data.metrics.get("completion_tokens", 0)
    total_tokens.append(pt + ct)

# Plot with matplotlib or your preferred tool
```

---

## Example Dashboards

### Cost Analysis

| Metric | Query |
|---|---|
| Total cost per repo | `SELECT repository, SUM(accumulated_cost) FROM runs GROUP BY repository` |
| Cost by trigger type | `SELECT trigger, AVG(accumulated_cost) FROM runs GROUP BY trigger` |
| Daily cost trend | `SELECT DATE(start_time), SUM(accumulated_cost) FROM runs GROUP BY 1` |

### Token Usage

| Metric | Query |
|---|---|
| Avg tokens per conversation | `SELECT AVG(prompt_tokens + completion_tokens) FROM runs` |
| Cache hit rate | `SELECT SUM(cache_read_tokens) / SUM(cache_write_tokens + cache_read_tokens) FROM runs` |
| Token ratio by model | `SELECT llm_model, AVG(prompt_tokens), AVG(completion_tokens) FROM runs GROUP BY llm_model` |

### Performance

| Metric | Query |
|---|---|
| Avg duration | `SELECT AVG(duration_seconds) FROM runs` |
| Duration by trigger | `SELECT trigger, AVG(duration_seconds) FROM runs GROUP BY trigger` |
| Success rate | `SELECT status, COUNT(*) FROM runs GROUP BY status` |

---

## Troubleshooting

### MLflow Not Starting

```bash
# Check if MLflow is installed
pip list | grep mlflow

# Test MLflow server manually
python -m mlflow server --host 0.0.0.0 --port 5000
```

### Metrics Not Appearing

1. **Check environment:** Verify `MLFLOW_TRACKING_URI` is set correctly
2. **Check logs:** Look for `MLflow` entries in OpenHands app server logs
3. **Run a conversation:** MLflow only creates runs when conversations start
4. **Check network:** Ensure OpenHands can reach the MLflow server

### MLflow Causing Issues

MLflow failures are always isolated:
- Errors are logged but never raised
- Conversations continue normally if MLflow is unavailable
- MLflow being down does not affect conversation execution

### Enable Debug Logging

```bash
export LOG_LEVEL=DEBUG
```

Or add to your Python logger config:
```python
logging.getLogger("openhands.app_server.services.mlflow_tracker").setLevel(logging.DEBUG)
```

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `MLFLOW_TRACKING_URI` | Yes | — | MLflow server URL (e.g., `http://localhost:5000`) |
| `MLFLOW_EXPERIMENT_NAME` | No | `openhands-conversations` | MLflow experiment name |

MLflow is **disabled** when `MLFLOW_TRACKING_URI` is not set or empty.

---

## Implementation Details

### Thread Safety

The `MLflowTracker` uses:
- `threading.Lock` for all run state access
- Per-conversation state stored in a dict keyed by conversation ID
- No global MLflow run state (each conversation has its own run)

### Error Isolation

All MLflow operations are wrapped in try/except blocks:
- `initialize()`: Disables MLflow on failure, logs warning
- `start_conversation()`: Logs warning, returns False
- `log_metrics()`: Logs warning, returns False
- `end_conversation()`: Logs warning, returns False

### No Hardcoded Values

All metrics come from actual OpenHands `Metrics` and `TokenUsage` objects.
No duplicate metric calculations are performed — the implementation uses
existing metrics generated by OpenHands LLM tracking.

---

## Files Changed

| File | Change |
|---|---|
| `openhands/app_server/services/mlflow_tracker.py` | **NEW** — MLflow tracker service |
| `openhands/app_server/event_callback/webhook_router.py` | Modified — MLflow lifecycle hooks |
| `openhands/app_server/app_conversation/sql_app_conversation_info_service.py` | Modified — Fixed `total_tokens` persistence |
| `openhands/app_server/MLFLOW.md` | Updated — Documentation |

---

## Validation Steps

1. **Deploy MLflow:** Start MLflow server and verify it's accessible
2. **Configure OpenHands:** Set `MLFLOW_TRACKING_URI` environment variable
3. **Start OpenHands:** Verify MLflow initialization appears in logs
4. **Run a GUI conversation:** Create a conversation via the web UI
5. **Verify MLflow run:** Check MLflow UI for a new run with logged metrics
6. **Run an automation:** Create an automation-triggered conversation
7. **Verify automation run:** Check MLflow UI for automation run with correct trigger parameter
8. **Check metrics:** Verify token counts and cost reflect actual LLM usage
9. **Test failure handling:** Stop MLflow server and verify conversations continue normally
