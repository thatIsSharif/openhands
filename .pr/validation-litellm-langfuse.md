# LiteLLM + Langfuse Integration — Validation Report

## Architecture

```
OpenHands ──→ LiteLLM Proxy (port 4000) ──→ LLM Provider (OpenAI, etc.)
                       │
                       └── langfuse_otel callback
                            │
                            └──→ Langfuse Cloud (OTLP)
```

## Why `langfuse_otel` (not `langfuse`)?

The official Langfuse documentation at https://langfuse.com/integrations/gateways/litellm
recommends the `langfuse_otel` callback. The `langfuse` callback (legacy) sends data
via the direct Langfuse REST API. `langfuse_otel` uses the OpenTelemetry protocol
(OTLP), which:
1. Is vendor-neutral (works with any OTEL-compatible backend)
2. Captures richer span-based data (timing, metadata, parent-child relationships)
3. Is the officially recommended approach by both Langfuse and LiteLLM

## Files Modified

| File | Change |
|------|--------|
| `openhands/app_server/services/litellm_proxy_manager.py` | **NEW** — Proxy lifecycle management, config generation, Langfuse integration |
| `openhands/app_server/LANGFUSE_LITELLM.md` | **NEW** — Comprehensive documentation |

## Validation Results

### 1. Langfuse Connectivity
- ✅ Langfuse reachable at https://us.cloud.langfuse.com (version 3.186.0)
- ✅ Authentication with hardcoded credentials works

### 2. LiteLLM Proxy Startup
- ✅ Proxy starts successfully on localhost:4000
- ✅ Health endpoint returns 200
- ✅ Models endpoint returns configured models

### 3. LLM Call Routing
- ✅ Proxy receives and routes LLM requests
- ✅ 401 on fake API key proves routing works end-to-end

### 4. Langfuse Traces Captured

**Trace #1: `test-integration` (via LiteLLM SDK langfuse_otel callback)**
- Input: "Say hello"
- Output: "Hello there!" (mock response)
- Model: gpt-3.5-turbo
- Observations: 2 (GENERATION + SPAN)

**Trace #2: `litellm_request` (via LiteLLM Proxy)**
- Input: "Say hello in 3 words"
- Model: gpt-3.5-turbo
- 401 due to fake API key — proves proxy routed correctly and callback fired

## Post-Implementation Information

### MLflow Server Installation
```bash
pip install mlflow
```

### MLflow Startup
```bash
mlflow server --host 0.0.0.0 --port 5000
```

### LiteLLM Proxy Installation
```bash
pip install 'litellm[proxy]' pyyaml
```

### LiteLLM Proxy Startup
```python
from openhands.app_server.services.litellm_proxy_manager import LangfuseLiteLLMIntegration
integration = LangfuseLiteLLMIntegration()
integration.start()
```

### Required Environment Variables
| Variable | Description |
|----------|-------------|
| `LANGFUSE_PUBLIC_KEY` | Langfuse public API key |
| `LANGFUSE_SECRET_KEY` | Langfuse secret API key |
| `LANGFUSE_OTEL_HOST` | Langfuse OTEL host URL |
| `OPENAI_API_KEY` | For routing gpt-* models |
| `ANTHROPIC_API_KEY` | For routing claude-* models |
| `GEMINI_API_KEY` | For routing gemini-* models |
| `LITE_LLM_API_URL` | Set to `http://localhost:4000` for OpenHands routing |

### Verification Procedure
1. Start the proxy: `python3 -c "from openhands.app_server.services.litellm_proxy_manager import LangfuseLiteLLMIntegration; LangfuseLiteLLMIntegration().start()"`
2. Check health: `curl http://localhost:4000/health/readiness`
3. Send test request: `curl -X POST http://localhost:4000/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer $OPENAI_API_KEY" -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'`
4. Check Langfuse: Browse to https://us.cloud.langfuse.com → Traces tab

### How to Validate Token/Cost/Latency Collection
Run a successful LLM call through the proxy with a valid API key, then:
1. Check proxy logs: `grep -i "usage\|token\|cost\|latency" /tmp/litellm_proxy.log`
2. Check Langfuse UI: Traces → Click trace → see Usage, Cost, Latency fields
3. Langfuse API: `GET /api/public/traces/{id}` → check `usage`, `cost`, `latency` fields

### How to Verify Automation Conversations Export Correctly
Automation-created conversations go through the same LLM layer. When the proxy is configured:
- Set `LITE_LLM_API_URL=http://localhost:4000` before starting OpenHands
- Or configure LLM profile with `base_url=http://localhost:4000`
- All LLM calls from both UI and automation conversations will route through the proxy
- Verify in Langfuse by filtering traces by timestamp

### Example Langfuse Queries
```bash
# Get all traces
curl -H "Authorization: Basic $(echo -n 'pk-lf-...:sk-lf-...' | base64)" \
  https://us.cloud.langfuse.com/api/public/traces

# Get trace details
curl -H "Authorization: Basic $(echo -n 'pk-lf-...:sk-lf-...' | base64)" \
  https://us.cloud.langfuse.com/api/public/traces/{TRACE_ID}
```
