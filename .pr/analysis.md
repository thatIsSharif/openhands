# Metrics Persistence Investigation

## Root Cause Analysis

### Bug: `total_tokens` Hardcoded to Zero

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
