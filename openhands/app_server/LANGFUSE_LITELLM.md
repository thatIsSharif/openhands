# Langfuse + LiteLLM Integration for OpenHands

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      OpenHands                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              LLM Configuration                        │   │
│  │  model: gpt-4o | claude-opus-4-6 | ...               │   │
│  │  base_url: http://localhost:4000                      │   │
│  │  api_key: <provider-api-key>                         │   │
│  └────────────────────┬─────────────────────────────────┘   │
│                       │                                       │
│                       │ HTTP (OpenAI-compatible)              │
└───────────────────────┼───────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                  LiteLLM Proxy (port 4000)                    │
│                                                               │
│  ┌─────────────────┐   ┌────────────────────────────────┐   │
│  │  Model Router    │   │  Callbacks: ["langfuse_otel"]  │   │
│  │                  │   │                                 │   │
│  │  gpt-*  → OpenAI │   │  Captures:                     │   │
│  │  claude-*→Anthro │   │  • Prompt/Completion/Output    │   │
│  │  gemini-*→Google │   │  • Token counts + Cost         │   │
│  │  * → passthrough │   │  • Latency + Model             │   │
│  └────────┬─────────┘   └───────────────┬─────────────────┘   │
│           │                             │                     │
└───────────┼─────────────────────────────┼─────────────────────┘
            │                             │
            ▼                             ▼
   ┌────────────────┐         ┌──────────────────────┐
   │  LLM Provider   │         │    Langfuse Cloud     │
   │  (OpenAI, etc.) │         │  us.cloud.langfuse.com│
   │                 │         │                      │
   │  → Real API call│         │  → OTEL traces       │
   │  → Response     │         │  → Token metrics      │
   └────────────────┘         │  → Cost data          │
                               │  → Full I/O logging   │
                               └──────────────────────┘
```

## Why LiteLLM Proxy?

The OpenHands team's [recommended approach](
https://github.com/OpenHands/OpenHands/issues/9579) for LLM observability is
to use a **LiteLLM Proxy** sidecar. This keeps OpenHands vendor-neutral while
allowing any observability backend (Langfuse, Langsmith, etc.) to be plugged
in at the proxy layer.

## Why `langfuse_otel`?

The Langfuse + LiteLLM integration supports two callbacks:

| Callback | Status | Protocol | Recommendation |
|----------|--------|----------|----------------|
| `langfuse` | Legacy | Direct Langfuse REST API | Deprecated |
| `langfuse_otel` | **Recommended** | OpenTelemetry (OTLP) | ✅ **Use this** |

**`langfuse_otel`** is the officially recommended callback because:
1. Uses the vendor-neutral OpenTelemetry protocol
2. Captures richer data (spans, timing, metadata)
3. Supported by both [Langfuse](https://langfuse.com/integrations/gateways/litellm)
   and [LiteLLM](https://docs.litellm.ai/docs/observability/langfuse_otel_integration)
4. Future-proof — OTEL is the industry standard for observability

## Files Modified

| File | Purpose |
|------|---------|
| `openhands/app_server/services/litellm_proxy_manager.py` | **New** — Proxy lifecycle management, config generation, Langfuse integration |
| `openhands/app_server/LANGFUSE_LITELLM.md` | **New** — This documentation |

## How Tracing Works End-to-End

### 1. Proxy Startup

1. `LangfuseLiteLLMIntegration.start()` is called
2. The module generates `litellm_config.yaml` with:
   - Model routing rules (wildcards for gpt-*, claude-*, gemini-*, etc.)
   - `callbacks: ["langfuse_otel"]` — the Langfuse OpenTelemetry callback
3. Hardcoded Langfuse credentials are injected as environment variables:
   - `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_OTEL_HOST`
   - `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`
4. The `litellm` CLI starts as a subprocess

### 2. Request Flow

1. OpenHands sends an OpenAI-compatible request to `http://localhost:4000/v1/...`
2. LiteLLM Proxy matches the model to a routing rule
3. Proxy forwards the request to the real provider (OpenAI, Anthropic, etc.)
4. Provider returns the response
5. **Before returning**, the `langfuse_otel` callback fires:
   - Captures: model name, input messages, output, token counts, cost, latency
   - Sends OTEL trace to Langfuse via `OTEL_EXPORTER_OTLP_ENDPOINT`
6. Response is returned to OpenHands

### 3. Data Captured in Langfuse

| Field | Source | Example |
|-------|--------|---------|
| Prompt/Input | Request body | `[{"role":"user","content":"Hello"}]` |
| Completion/Output | Response body | `"Hi there!"` |
| Model name | Request `model` field | `gpt-4o` |
| Prompt tokens | Response `usage.prompt_tokens` | 42 |
| Completion tokens | Response `usage.completion_tokens` | 10 |
| Total tokens | Response `usage.total_tokens` | 52 |
| Cost | Calculated from token usage | $0.00085 |
| Latency | Request timing | 1.2s |
| Duration | Wall-clock time | 1.5s |

## Setup

### Prerequisites

```bash
pip install 'litellm[proxy]'                       # LiteLLM proxy server
pip install opentelemetry-api opentelemetry-sdk     # Already installed
pip install opentelemetry-exporter-otlp             # OTEL exporter for Langfuse
pip install pyyaml                                  # Config file writing
```

### Quick Start (Programmatic)

```python
from openhands.app_server.services.litellm_proxy_manager import (
    LangfuseLiteLLMIntegration,
    configure_openhands_for_proxy,
)

# Start the LiteLLM proxy with Langfuse tracing
integration = LangfuseLiteLLMIntegration()
integration.start()

# Configure OpenHands to route through the proxy
configure_openhands_for_proxy()

# Now all LLM calls go through the proxy → Langfuse

# Cleanup
integration.stop()
```

### Quick Start (Manual)

```bash
# 1. Set Langfuse environment variables
export LANGFUSE_PUBLIC_KEY="pk-lf-6c9b4b03-80c5-4c5d-8565-b681f02d2c71"
export LANGFUSE_SECRET_KEY="sk-lf-2adf9dc1-8582-4d1d-8eeb-1ef5b5c4e1e8"
export LANGFUSE_OTEL_HOST="https://us.cloud.langfuse.com"
export LITELLM_LOG="DEBUG"  # Optional: verbose logging

# 2. Start the proxy
litellm --config /path/to/litellm_config.yaml --port 4000 --host 0.0.0.0

# 3. In OpenHands settings, set:
#    model: <any-model>
#    base_url: http://localhost:4000
#    api_key: <your-provider-api-key>
```

### Integration with OpenHands

In your LLM profile settings, configure:

```json
{
  "model": "gpt-4o",
  "base_url": "http://localhost:4000",
  "api_key": "sk-your-openai-api-key"
}
```

Or use the `LITE_LLM_API_URL` environment variable (for `openhands/` prefixed models):

```bash
export LITE_LLM_API_URL=http://localhost:4000
```

## Verification

### Check Proxy Health

```bash
curl -s http://localhost:4000/health/readiness | python3 -m json.tool
```

Expected: `{"status": "OK"}`

### List Available Models

```bash
curl -s http://localhost:4000/v1/models \
  -H "Authorization: Bearer sk-test" | python3 -m json.tool
```

### Test Chat Completion

```bash
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Say hello in 3 words"}],
    "max_tokens": 20
  }' | python3 -m json.tool
```

### Verify Langfuse Traces

```python
import base64, json, urllib.request

auth = base64.b64encode(
    b"pk-lf-6c9b4b03-80c5-4c5d-8565-b681f02d2c71"
    b":sk-lf-2adf9dc1-8582-4d1d-8eeb-1ef5b5c4e1e8"
).decode()

req = urllib.request.Request(
    "https://us.cloud.langfuse.com/api/public/traces?limit=5",
    headers={"Authorization": f"Basic {auth}"},
)
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    for trace in data["data"]:
        print(f"Trace: {trace['id']} — {trace.get('name', '(unnamed)')}")
```

### Viewing Traces in Langfuse UI

1. Go to [https://us.cloud.langfuse.com](https://us.cloud.langfuse.com)
2. Log in or use the API keys above
3. Navigate to **Traces** tab
4. Look for traces named `litellm_request` or filter by model name
5. Click a trace to see:
   - Full input/output (Prompt, Completion)
   - Token usage (Prompt tokens, Completion tokens, Total tokens)
   - Cost
   - Latency
   - Model name
   - Metadata (tags, user ID, etc.)

## Troubleshooting

### "No traces appear in Langfuse"

1. **Check environment variables** — Verify Langfuse credentials are set:
   ```bash
   echo "Public: ${LANGFUSE_PUBLIC_KEY:0:20}..."
   echo "Secret: ${LANGFUSE_SECRET_KEY:0:20}..."
   ```

2. **Check proxy logs** — Look for callback initialization messages:
   ```bash
   grep -i langfuse /tmp/litellm_proxy.log
   ```

3. **Enable debug logging**:
   ```bash
   export LITELLM_LOG=DEBUG
   ```

4. **Test Langfuse connectivity directly**:
   ```bash
   python3 -c "
   import base64, urllib.request
   auth = base64.b64encode(b'pk-lf-...:sk-lf-...').decode()
   req = urllib.request.Request('https://us.cloud.langfuse.com/api/public/health',
       headers={'Authorization': f'Basic {auth}'})
   with urllib.request.urlopen(req) as r:
       print(r.status, r.read().decode())
   "
   ```

### "Proxy fails to start"

1. Check the proxy log: `cat /tmp/litellm_proxy.log`
2. Verify all dependencies:
   ```bash
   pip install 'litellm[proxy]' opentelemetry-exporter-otlp pyyaml
   ```
3. Check if the port is already in use:
   ```bash
   lsof -i :4000
   ```

### "OpenHands is not routing through the proxy"

1. Verify the proxy is running: `curl -s http://localhost:4000/health/readiness`
2. Check that `base_url` is set correctly in your LLM configuration
3. For `openhands/` models, ensure `LITE_LLM_API_URL` is set

### "Tokens/Cost showing as 0"

1. This usually means the API call failed before getting a response
2. Check the proxy logs for error messages
3. Verify the API key is correct for the provider

## Docker Deployment

For Docker deployments, run LiteLLM Proxy as a sidecar container:

```yaml
version: "3.8"
services:
  openhands:
    image: ghcr.io/openhands/openhands
    environment:
      - LITE_LLM_API_URL=http://litellm-proxy:4000
    depends_on:
      - litellm-proxy

  litellm-proxy:
    image: ghcr.io/berriai/litellm:main-latest
    ports:
      - "4000:4000"
    volumes:
      - ./litellm_config.yaml:/app/config.yaml
    environment:
      - LANGFUSE_PUBLIC_KEY=pk-lf-...
      - LANGFUSE_SECRET_KEY=sk-lf-...
      - LANGFUSE_OTEL_HOST=https://us.cloud.langfuse.com
    command: --config /app/config.yaml --port 4000 --host 0.0.0.0
```

## Example: Langfuse Dashboard Queries

### All traces for a specific conversation:

```
POST https://us.cloud.langfuse.com/api/public/traces
{
  "tags": ["conversation_id:abc-123"]
}
```

### Cost by model:

```
GET https://us.cloud.langfuse.com/api/public/metrics?from=2026-01-01&to=2026-12-31

        ┌──────────────────────────────────────┐
        │  Cost by Model (Langfuse Dashboard)   │
        ├──────────────────┬───────────────────┤
        │  gpt-4o          │        $12.50     │
        │  claude-opus-4-6 │         $8.30     │
        │  gpt-4o-mini     │         $2.10     │
        └──────────────────┴───────────────────┘
```

### Token usage over time:

```
GET https://us.cloud.langfuse.com/api/public/traces?page=1&limit=50

For each trace, extract:
  - promptTokens + completionTokens = totalTokens
  - cost (pre-calculated by LiteLLM)
  - model
```

## Appendix: Configuration Reference

### LiteLLM Proxy Config Structure

```yaml
model_list:
  # OpenAI models
  - model_name: "gpt-*"
    litellm_params:
      model: "gpt-*"
      api_key: os.environ/OPENAI_API_KEY

  # Anthropic models
  - model_name: "claude-*"
    litellm_params:
      model: "claude-*-*"
      api_key: os.environ/ANTHROPIC_API_KEY

  # Google models
  - model_name: "gemini-*"
    litellm_params:
      model: "gemini/*"
      api_key: os.environ/GEMINI_API_KEY

  # Catch-all passthrough (any model/providers LiteLLM supports)
  - model_name: "*"
    litellm_params:
      model: "*"

litellm_settings:
  callbacks: ["langfuse_otel"]
  set_verbose: true

general_settings:
  pass_through: true
```

### Environment Variables

| Variable | Required | Description | Default |
|----------|----------|-------------|---------|
| `LITE_LLM_API_URL` | For routing | OpenHands proxy base URL | `https://llm-proxy.app.all-hands.dev` |
| `LANGFUSE_PUBLIC_KEY` | Yes | Langfuse public API key | (hardcoded in module) |
| `LANGFUSE_SECRET_KEY` | Yes | Langfuse secret API key | (hardcoded in module) |
| `LANGFUSE_OTEL_HOST` | Yes | Langfuse OTEL endpoint | `https://us.cloud.langfuse.com` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Auto-set | Full OTEL endpoint URL | Computed from `LANGFUSE_OTEL_HOST` |
| `OTEL_EXPORTER_OTLP_HEADERS` | Auto-set | Basic auth header for Langfuse | Base64-encoded credentials |
| `OPENAI_API_KEY` | Per model | OpenAI API key | (user-provided) |
| `ANTHROPIC_API_KEY` | Per model | Anthropic API key | (user-provided) |
| `LITELLM_LOG` | Optional | Proxy log level | `INFO` |
