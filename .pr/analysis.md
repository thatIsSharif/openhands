# Metrics Persistence Investigation

## Root Cause Analysis - Debugging Investigation (June 2026)

### Bug 0: `_info()` / `_debug()` / `_warn()` / `_error()` logger wrappers use `**kwargs` instead of `*args`

**File:** `openhands/app_server/services/mlflow_tracker.py`

**Severity: CRITICAL**

The logging wrapper functions were defined as `def _info(msg: str, **kwargs)`
but called with positional format args: `_info('uri=%s experiment=%s', uri, name)`.
This caused `TypeError: _info() takes 1 positional argument but 3 were given`
during `initialize()`, which set `self._enabled = False`.

This means the MLflowTracker immediately disabled itself on any call site
using the standard logging `%s` format syntax. Any conversation that triggered
a format-string log call would silently disable MLflow.

**Fix:** Changed to `def _info(msg: str, *args, **kwargs)` to match the standard
`logging.info(msg, *args, **kwargs)` signature.

### Bug 1: `total_tokens` Hardcoded to Zero

**File:** `openhands/app_server/app_conversation/sql_app_conversation_info_service.py`

**Line 367 (before fix):**
```python
total_tokens=0,  # HARDCODED
```

The `save_app_conversation_info()` method creates a `StoredConversationMetadata` row with
`total_tokens=0` hardcoded instead of computing it from `prompt_tokens + completion_tokens`.
This means:
- When a new conversation is saved, `total_tokens` is always 0
- Even if `update_conversation_statistics()` correctly updates `prompt_tokens` and
  `completion_tokens`, `total_tokens` remains 0 because that method does not update it

### Bug: `total_tokens` Not Updated in `update_conversation_statistics`

**File:** `openhands/app_server/app_conversation/sql_app_conversation_info_service.py`

The `update_conversation_statistics()` method (lines 390-470) updates `prompt_tokens`,
`completion_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, etc.,
but never computes or updates `total_tokens` from `prompt_tokens + completion_tokens`.

### Stats Events May Not Fire for In-Place Mutations

The `ConversationState.__setattr__` mechanism in the SDK emits
`ConversationStateUpdateEvent` only when a public field is **reassigned**, not when a
mutable field is mutated in-place. `ConversationState.stats` is a `ConversationStats`
object whose `usage_to_metrics` dict is mutated in-place when LLM metrics are accumulated.
This means `ConversationStateUpdateEvent(key='stats')` may not be emitted during normal
operation, so the `process_stats_event()` path in `webhook_router.py` could be a no-op.

However, the `on_conversation_update()` webhook (called on conversation start/pause/resume)
reads `conversation_info.stats.get_combined_metrics()` directly from the agent server,
which does contain the correct accumulated metrics at that point. This provides a secondary
path for metrics capture.

### Fix Applied

1. **`save_app_conversation_info`**: Changed `total_tokens=0` to
   `total_tokens=usage.prompt_tokens + usage.completion_tokens`
2. **`update_conversation_statistics`**: Added logic to recompute `total_tokens` whenever
   `completion_tokens` is updated and `prompt_tokens` is available

### Metrics Flow (After Fix)

```
SDK LLM.completion()
  → Metrics.add_cost(), Metrics.add_token_usage()
  → LLM.metrics mutated in-place (reference stored in ConversationStats.usage_to_metrics)

Agent Server → App Server Webhook:
  POST /webhooks/events/{id}:
    → ConversationStateUpdateEvent(key='stats') — if emitted:
        → process_stats_event() → update_conversation_statistics()
          → DB updated with correct total_tokens
  
  POST /webhooks/conversations:
    → conversation_info.stats.get_combined_metrics() — always has correct values
    → save_app_conversation_info() — now correctly computes total_tokens

  _track_conversation_terminal():
    → Reads app_conversation_info.metrics from DB
    → If stats events were processed: metrics are correct
    → If stats events were NOT processed: metrics are initial zeros
```

### Automation Conversations

Both UI-created and automation-created conversations follow the same webhook paths:
- `on_conversation_update()` is called with the full `ConversationInfo` payload
- `on_event()` is called with event batches
- `_track_conversation_terminal()` fires on terminal execution status

The only difference is the `trigger` value (`ConversationTrigger.AUTOMATION` vs
`ConversationTrigger.GUI`), which is correctly preserved.

## MLflow Implementation

### Architecture

The `MLflowTracker` service (`openhands/app_server/services/mlflow_tracker.py`) uses a
thread-safe, per-conversation run registry pattern:

- **Thread Safety**: `threading.Lock` for all run state access
- **Concurrent Conversations**: Each conversation has its own `ConversationRunState`
- **No Global State**: No global MLflow run tracking — each conversation manages its own run
- **Graceful Degradation**: All MLflow errors are caught and logged; failures never affect
  conversation execution

### Integration Points (webhook_router.py)

1. `on_conversation_update()` — Start MLflow run when `is_new_conversation` is True
2. `on_event()` — Log metrics when `ConversationStateUpdateEvent(key='stats')` is processed
3. `_track_conversation_terminal()` — Log final metrics and end MLflow run

### Design Decisions

- MLflow import is deferred to each method to avoid hard dependency
- Environment variable `MLFLOW_TRACKING_URI` gates all MLflow activity
- No mlflow dependency added to pyproject.toml (optional, user-installed)
- No enterprise code modified

---

## Debug Investigation Findings (June 2026)

### Bug A (CRITICAL - FIXED): Logger wrappers don't accept \`*args\`

**File:** `openhands/app_server/services/mlflow_tracker.py`

The `_info()`, `_debug()`, `_warn()`, `_error()` helper functions used
`def fn(msg, **kwargs)` but call sites used `fn('fmt %s', arg)` (positional
format args). This caused `TypeError` during `initialize()` which permanently
disabled the tracker.

**Evidence:** Verified by inserting a call to `_info('uri=%s', uri)` which
raised `TypeError: _info() takes 1 positional argument but 3 were given`.
The exception was caught by the generic `except Exception` in `initialize()`,
setting `self._enabled = False`.

**Fix:** Changed signatures to `def fn(msg, *args, **kwargs)`.

### Bug B (HIGH - FIXED): \`_track_conversation_terminal\` uses stale metrics

**File:** `openhands/app_server/event_callback/webhook_router.py`, lines 127-138

The function reads `app_conversation_info.metrics` which is always `None`
for conversations where no metrics were explicitly set at creation.
`_to_info()` in `sql_app_conversation_info_service.py` DOES rebuild
`MetricsSnapshot` from stored DB columns, but `valid_conversation()`
dependency injection runs before `process_stats_event()` updates the DB.

**Evidence:** Code inspection shows `app_conversation_info.metrics was None`
consistently for new conversations. The `if metrics:` check in the final
MLflow logging block was always False.

**Fix:** Added a fallback DB query in `_track_conversation_terminal`:
if `app_conversation_info.metrics` is None/zero, re-fetch from the DB
via `app_conversation_info_service.get_app_conversation_info()`.

### Bug C (MEDIUM - FIXED): No \`MLFLOW_ENABLED\` env var support

**File:** `openhands/app_server/services/mlflow_tracker.py`

`is_mlflow_enabled()` only checked `MLFLOW_TRACKING_URI`. Users might set
`MLFLOW_ENABLED=true` expecting it to work.

**Fix:** Added `MLFLOW_ENABLED` check (accepts 'true' or '1').

### Bug D (OBSERVED): MLflow 3.x rejects \`file://\` URIs

MLflow 3.x requires `sqlite:///` for local tracking or `http://` for a server.
The error is: "The filesystem tracking backend is in maintenance mode".

**Fix:** Documentation updated to recommend `sqlite:///mlflow.db` or
`http://localhost:5000`.

### Root Cause: Why No Data Appears in MLflow UI

**The most likely production cause:** `MLFLOW_TRACKING_URI` environment variable
not set in the app server process. Without this, `is_mlflow_enabled()` returns
False and no MLflow operations are performed.

**Second most likely cause:** `initialize()` failed (due to Bug A or MLflow 3.x
file URI rejection), setting `_enabled = False` permanently (singleton, no retry).

**Event deserialization:** ✅ Verified working end-to-end. `ConversationStateUpdateEvent`
with `key='stats'` correctly deserializes via `Event.model_validate()`.

**MLflowTracker lifecycle:** ✅ Verified working end-to-end with SQLite backend.
`start_run()`, `log_metrics()`, `end_run()` all succeed. MLflow API queries
confirm runs, metrics, and params are stored.

**Stats webhook flow:** The agent server's `_setup_stats_streaming()` (event_service.py:800)
installs callbacks that fire on every LLM completion. Stats are emitted as
`ConversationStateUpdateEvent(key='stats')` via the pub/sub → webhook pipeline.
This mechanism is present in openhands-agent-server==1.28.0 and verified by
code inspection.

