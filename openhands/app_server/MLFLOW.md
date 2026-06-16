MLflow Integration with OpenHands: Complete Guide
What Can You Achieve with MLflow?

1. Metrics Tracking ✅ Perfect Fit
OpenHands Data	MLflow Method
Input/Output tokens	mlflow.log_metrics()
Latency	mlflow.log_metrics()
Cost	mlflow.log_metrics()
Cache hits	mlflow.log_metrics()
Reasoning tokens	mlflow.log_metrics()


2. Parameters/Config Logging ✅ Perfect Fit
Model name, temperature, max tokens
Condenser settings
Repository, branch, trigger type
Skills loaded


3. Artifacts ✅ Good Fit
Conversation summaries
Generated code files
Task outputs
Screenshots (if enabled)


4. Traces/Spans ⚠️ Limited
MLflow tracing is more for LLM calls within a pipeline
OpenHands uses Laminar for fine-grained spans (optional)


5. Evaluation ✅ Good Fit
Compare runs across repositories
Track task completion rates
Cost efficiency analysis
Integration Approach
Option A: Minimal Integration (Recommended for Start)



openhands_mlflow_integration.py

import mlflow
from mlflow import log_metric, log_params, log_text
from openhands.sdk.llm.utils.metrics import Metrics, TokenUsage

class OpenHandsMLflowTracker:
"""Simple MLflow tracker for OpenHands conversations."""

def __init__(self, tracking_uri: str = "http://localhost:5000"):
    mlflow.set_tracking_uri(tracking_uri)
    self.experiment_name = "openhands-conversations"
    mlflow.set_experiment(self.experiment_name)

def start_conversation_run(self, conversation_id: str, metadata: dict):
    """Start a run for a new conversation."""
    mlflow.start_run(run_name=conversation_id)
    mlflow.log_params({
        "conversation_id": conversation_id,
        "repository": metadata.get("selected_repository", "none"),
        "branch": metadata.get("selected_branch", "main"),
        "trigger": metadata.get("trigger", "gui"),
        "llm_model": metadata.get("llm_model", "unknown"),
    })

def log_conversation_metrics(self, metrics: Metrics):
    """Log token usage and cost metrics."""
    mlflow.log_metrics({
        "prompt_tokens": metrics.accumulated_token_usage.prompt_tokens,
        "completion_tokens": metrics.accumulated_token_usage.completion_tokens,
        "cache_read_tokens": metrics.accumulated_token_usage.cache_read_tokens,
        "cache_write_tokens": metrics.accumulated_token_usage.cache_write_tokens,
        "reasoning_tokens": metrics.accumulated_token_usage.reasoning_tokens,
        "total_cost": metrics.accumulated_cost,
        "context_window": metrics.accumulated_token_usage.context_window,
    })

def log_latency(self, latency_seconds: float):
    """Log response latency."""
    mlflow.log_metric("latency_seconds", latency_seconds)

def log_conversation_summary(self, summary: str):
    """Log conversation summary as artifact."""
    mlflow.log_text(summary, "conversation_summary.txt")

def end_run(self):
    """End the current MLflow run."""
    mlflow.end_run()

Option B: Callback-Based Integration (Production Ready)

openhands_mlflow_callback.py

from openhands.sdk.event import Event, ConversationStateUpdateEvent
from openhands.sdk.conversation import ConversationExecutionStatus
import mlflow
from datetime import datetime

class MLflowConversationCallback:
"""
Integrate MLflow tracking into OpenHands conversation lifecycle.

Usage:
    callback = MLflowConversationCallback(tracking_uri="http://mlflow:5000")
    # Register with conversation service
"""

def __init__(self, tracking_uri: str, experiment_name: str = "openhands"):
    self.tracking_uri = tracking_uri
    self.experiment_name = experiment_name
    self._setup_mlflow()

def _setup_mlflow(self):
    mlflow.set_tracking_uri(self.tracking_uri)
    mlflow.set_experiment(self.experiment_name)
    mlflow.enable_system_metrics_logging()

def on_conversation_start(self, conversation_id: str, metadata: dict):
    """Called when a new conversation starts."""
    self.run = mlflow.start_run(run_name=f"conv_{conversation_id}")

    # Log conversation metadata
    mlflow.log_params({
        "conversation_id": conversation_id,
        "start_time": datetime.utcnow().isoformat(),
        "repository": metadata.get("selected_repository", ""),
        "branch": metadata.get("selected_branch", ""),
        "trigger": metadata.get("trigger", ""),
        "agent_type": metadata.get("agent_type", "default"),
    })

def on_stats_update(self, metrics: dict):
    """Called periodically with updated metrics."""
    mlflow.log_metrics({
        "prompt_tokens": metrics.get("prompt_tokens", 0),
        "completion_tokens": metrics.get("completion_tokens", 0),
        "cache_read_tokens": metrics.get("cache_read_tokens", 0),
        "cache_write_tokens": metrics.get("cache_write_tokens", 0),
        "accumulated_cost": metrics.get("accumulated_cost", 0.0),
        "total_tokens": metrics.get("total_tokens", 0),
    }, step=metrics.get("turn", 0))

def on_latency_measurement(self, turn: int, latency: float, tokens: int):
    """Log per-turn latency."""
    mlflow.log_metrics({
        f"turn_{turn}_latency": latency,
        f"turn_{turn}_tokens": tokens,
    })

def on_conversation_end(self, status: ConversationExecutionStatus, summary: str = ""):
    """Called when conversation completes."""
    mlflow.log_params({
        "end_time": datetime.utcnow().isoformat(),
        "status": status.value if status else "unknown",
    })

    if summary:
        mlflow.log_text(summary, "final_summary.txt")

    mlflow.end_run()

Where to Integrate in OpenHands
Integration Points
openhands/
├── app_server/
│   ├── app_conversation/
│   │   ├── app_conversation_service_base.py  ← Start/end runs
│   │   └── sql_app_conversation_info_service.py  ← Store MLflow run_id
│   └── event_callback/
│       └── webhook_router.py  ← Periodic metrics logging
Code Changes Required

1. Create the MLflow Service
File: openhands/app_server/services/mlflow_tracker.py (NEW)



"""MLflow tracking service for OpenHands conversations."""

import mlflow
from datetime import datetime
from typing import Optional
from uuid import UUID

class MLflowTracker:
"""MLflow tracker for OpenHands observability."""

def __init__(
    self,
    tracking_uri: str,
    experiment_name: str = "openhands-conversations",
):
    self.tracking_uri = tracking_uri
    self.experiment_name = experiment_name
    self._active_run_id: Optional[str] = None
    self._turn_count = 0

def initialize(self):
    """Initialize MLflow connection."""
    mlflow.set_tracking_uri(self.tracking_uri)
    mlflow.set_experiment(self.experiment_name)

def start_conversation(
    self,
    conversation_id: UUID,
    repository: Optional[str] = None,
    branch: Optional[str] = None,
    trigger: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> str:
    """Start MLflow run for a conversation. Returns run_id."""
    self._turn_count = 0
    run = mlflow.start_run(run_name=str(conversation_id))
    self._active_run_id = run.info.run_id

    mlflow.log_params({
        "conversation_id": str(conversation_id),
        "start_time": datetime.utcnow().isoformat(),
        "repository": repository or "none",
        "branch": branch or "main",
        "trigger": trigger or "unknown",
        "llm_model": llm_model or "unknown",
    })

    return self._active_run_id

def log_metrics(
    self,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    accumulated_cost: float = 0.0,
    latency_seconds: float = 0.0,
):
    """Log token and cost metrics."""
    if not self._active_run_id:
        return

    mlflow.log_metrics({
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "accumulated_cost": accumulated_cost,
        "latency_seconds": latency_seconds,
    }, step=self._turn_count)

def increment_turn(self, latency: float = 0.0):
    """Increment turn counter and log latency."""
    self._turn_count += 1
    mlflow.log_metrics({f"turn_{self._turn_count}_latency": latency})

def log_conversation_summary(self, summary: str):
    """Log final conversation summary."""
    if self._active_run_id:
        mlflow.log_text(summary, "conversation_summary.txt")

def end_conversation(self, status: str, error: Optional[str] = None):
    """End the conversation run."""
    if not self._active_run_id:
        return

    mlflow.log_params({
        "end_time": datetime.utcnow().isoformat(),
        "status": status,
        "total_turns": self._turn_count,
    })

    if error:
        mlflow.log_text(error, "error.txt")

    mlflow.end_run()
    self._active_run_id = None

2. Integrate with Conversation Service
File: openhands/app_server/app_conversation/app_conversation_service_base.py



Add integration in _start_conversation method:

In _start_conversation or similar method

from openhands.app_server.services.mlflow_tracker import MLflowTracker

class AppConversationServiceBase:
def init(self, ..., mlflow_tracker: MLflowTracker | None = None):
self.mlflow_tracker = mlflow_tracker

async def _start_conversation(self, ...):
    # Existing code...

    # Add MLflow tracking
    if self.mlflow_tracker:
        self.mlflow_tracker.start_conversation(
            conversation_id=conversation_id,
            repository=request.selected_repository,
            branch=request.selected_branch,
            trigger=request.trigger.value if request.trigger else None,
            llm_model=request.llm_model,
        )

    # Continue with existing flow...

3. Hook into Metrics Updates
File: openhands/app_server/event_callback/webhook_router.py



In on_event or stats processing:

When processing stats events

if self.mlflow_tracker and stats:
self.mlflow_tracker.log_metrics(
prompt_tokens=stats.usage_to_metrics.get("prompt_tokens", 0),
completion_tokens=stats.usage_to_metrics.get("completion_tokens", 0),
cache_read_tokens=stats.usage_to_metrics.get("cache_read_tokens", 0),
cache_write_tokens=stats.usage_to_metrics.get("cache_write_tokens", 0),
reasoning_tokens=stats.usage_to_metrics.get("reasoning_tokens", 0),
accumulated_cost=stats.accumulated_cost or 0.0,
)
MLflow Setup

1. Start MLflow Server



Option A: Local

mlflow server --backend-store-uri sqlite:///mlflow.db \
--default-artifact-root ./mlflow_artifacts \
--host 0.0.0.0 --port 5000

Option B: With Docker

docker run -p 5000:5000 \
-v $(pwd)/mlflow:/mlflow \
ghcr.io/mlflow/mlflow:latest \
mlflow server --backend-store-uri sqlite:////mlflow/mlflow.db \
--default-artifact-root /mlflow/artifacts

Option C: Remote (e.g., Databricks, etc.)

mlflow.set_tracking_uri("databricks://<profile>")
2. Environment Configuration

.env file

MLFLOW_TRACKING_URI=http://localhost:5000
MLFLOW_EXPERIMENT_NAME=openhands-conversations
MLFLOW_LOG_SYSTEM_METRICS=true
What You Can Analyze with MLflow

1. Cost Analysis Dashboard
┌─────────────────────────────────────────────────┐
│  Cost per Repository                            │
├─────────────────────────────────────────────────┤
│  owner/repo-a      ████████████  $12.45         │
│  owner/repo-b      ██████        $5.23          │
│  owner/repo-c      ████          $2.10          │
└─────────────────────────────────────────────────┘


2. Token Usage Trends
Average tokens per conversation
Token efficiency by model
Cache hit rate over time


3. Latency Analysis
Per-turn latency
Bottleneck identification
Model comparison


4. Trigger Analysis
Cost by trigger type (Jira, GitHub, GUI)
Success rates by trigger


5. Repository Comparison
┌──────────────────────────────────────────────────┐
│  Repository Efficiency (Cost/Task Completion)     │
├──────────────────────────────────────────────────┤
│  repo-a    3 tasks avg $4.50/task   ✓           │
│  repo-b    7 tasks avg $1.20/task   ✓✓           │
│  repo-c    2 tasks avg $8.50/task   ✗            │
└──────────────────────────────────────────────────┘
Easy Integration Checklist
Step	Action	Effort
1	Add mlflow to requirements.txt	1 min
2	Create MLflowTracker service class	30 min
3	Inject tracker into conversation service	15 min
4	Add log_metrics() in stats callback	15 min
5	Configure tracking URI	5 min
6	Start MLflow server	5 min
Total Time: ~1.5 hours for full integration



Comparison: MLflow vs Laminar vs Native DB
Feature	Native DB	MLflow	Laminar
Setup Complexity	✅ None	⚠️ Server needed	❌ API key needed
Query API	✅ REST	✅ REST + UI	✅ REST + UI
Metrics	✅ Basic	✅ Advanced	✅ Advanced
Traces	❌ None	⚠️ Basic	✅ Full
Visualization	❌ None	✅ Excellent	✅ Excellent
Cost Tracking	✅	✅	✅
Token Tracking	✅	✅	✅
Latency Tracking	✅	✅	✅
Self-Hosted	✅	✅	❌ Cloud
Open Source	✅	✅	❌ Proprietary
Recommendation
For your requirements (time, tokens, cost):

✅ MLflow is a great choice because:

Self-hosted - No external cloud dependencies
Simple setup - Single server, SQLite backend works
Excellent UI - Pre-built dashboards for metrics
Rich queries - Filter by repository, time, trigger
Artifact support - Store conversation summaries
Production ready - Used by thousands of companies
Start with:

pip install mlflow
mlflow server --backend-store-uri sqlite:///mlflow.db
Then add the MLflowTracker class to your OpenHands deployment.
