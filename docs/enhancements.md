# Recommended Enhancements

This document outlines the next-step engineering improvements recommended for the Autonomous Personnel Agent. Each enhancement is prioritised by impact and implementation effort.

---

## 1. Rate Limiting

**Why:** Without rate limiting, a misbehaving client or a leaked `API_TOKEN` can flood the LLM tier and run up costs. Rate limiting also helps attribute usage to specific callers.

**Approach:**

```python
# api/middleware/rate_limit.py
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(
    key_func=get_remote_address,   # or per-token: key_func=lambda req: req.headers.get("x-api-token", "anon")
    default_limits=["60/minute"],
    storage_uri="redis://localhost:6379",   # use Redis for distributed deployments
)

# In main.py:
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# On a specific route:
@app.post("/route")
@limiter.limit("20/minute")
async def route_request(request: Request, ...):
    ...
```

**Packages:** `slowapi>=0.1.9`, `redis>=5.0`

**Docker-compose addition:**

```yaml
redis:
  image: redis:7-alpine
  ports: ["6379:6379"]
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
```

**Per-token limiting:** Switch `key_func` to extract `x-api-token` so each caller gets their own quota rather than sharing an IP-level bucket — important in multi-tenant setups.

---

## 2. Structured JSON Logging

**Why:** Plain text logs are hard to aggregate in Datadog, CloudWatch, or Loki. Structured JSON lets you filter by `request_id`, `agent`, `status_code`, or `latency_ms` without regex.

**Approach using `structlog`:**

```python
# api/logging_config.py
import structlog, logging

def configure_logging(level: str = "INFO") -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

log = structlog.get_logger()
```

**Every log line becomes:**

```json
{
  "event": "request_complete",
  "request_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "method": "POST",
  "path": "/talent/screen",
  "status_code": 200,
  "latency_ms": 843,
  "agent": "talent",
  "timestamp": "2026-04-17T07:00:00Z"
}
```

**Packages:** `structlog>=24.0`

---

## 3. Request ID Middleware

**Why:** When a stakeholder reports "the 7:14 AM screening call failed," you need to be able to correlate all log lines for that single request — including agent sub-calls, DB queries, and LLM invocations.

**Approach:**

```python
# api/middleware/request_id.py
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
import structlog

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

# In main.py:
app.add_middleware(RequestIDMiddleware)
```

The `request_id` is automatically included in every `structlog` call via `merge_contextvars`. It is also echoed back in the response header so clients can include it in bug reports.

**Downstream propagation:** Pass `request_id` to `agents.py` as a parameter so it appears in LLM call logs too:

```python
log.info("llm_call", agent="talent", model="gpt-4o", tokens_used=312, request_id=request_id)
```

---

## 4. Async DB Connection Pool

**Why:** `psycopg2` is synchronous. Every DB query blocks an entire Uvicorn worker thread. At 10 concurrent requests that each query the DB, you starve the event loop. `asyncpg` + SQLAlchemy async releases the thread while waiting for Postgres.

**Approach:**

```python
# api/db.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"].replace("postgresql://", "asyncpg://")

engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,
)

AsyncSession = async_sessionmaker(engine, expire_on_commit=False)

async def get_session():
    async with AsyncSession() as session:
        yield session
```

**Packages:** `asyncpg>=0.29`, `sqlalchemy[asyncio]>=2.0`

**Migration note:** Replace `psycopg2-binary` in `requirements.txt` with `asyncpg`. Update `health.py` to use `asyncpg` for the latency probe.

---

## 5. Task Queue for Long-Running Agent Calls

**Why:** Agent calls that involve multiple LLM hops (Orchestrator → Talent → LLM) can take 5–15 seconds. This holds open HTTP connections and risks gateway timeouts at 30s. A task queue lets the API return a job ID immediately and process asynchronously.

**Approach using `arq` (async, Redis-backed, minimal overhead):**

```python
# api/worker.py
from arq import cron
from arq.connections import RedisSettings

async def run_talent_screen(ctx, payload: dict) -> dict:
    # ctx["agent"] is injected by arq worker startup
    return await ctx["talent_agent"].screen(payload)

class WorkerSettings:
    functions = [run_talent_screen]
    redis_settings = RedisSettings(host="redis", port=6379)
    max_jobs = 20

# api/main.py — non-blocking endpoint:
@app.post("/talent/screen/async")
async def talent_screen_async(request: TalentScreenRequest):
    job = await arq_pool.enqueue_job("run_talent_screen", request.dict())
    return {"job_id": job.job_id, "status": "queued"}

@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = ArqJob(job_id, arq_pool)
    result = await job.result(timeout=0)   # non-blocking poll
    return {"job_id": job_id, "status": job.status, "result": result}
```

**Packages:** `arq>=0.26`

**docker-compose addition:** Add the `worker` service running `python -m arq api.worker.WorkerSettings`.

---

## 6. Webhook Retry Queue with Exponential Backoff

**Why:** When the agent posts results to external webhooks (e.g. n8n, Slack), network blips cause silent failures. A retry queue with exponential backoff ensures delivery without manual re-runs.

**Approach:**

```python
# api/webhooks.py
import asyncio, httpx, structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = structlog.get_logger()

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
)
async def post_webhook(url: str, payload: dict, request_id: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers={"x-request-id": request_id})
        resp.raise_for_status()
    log.info("webhook_delivered", url=url, request_id=request_id)
```

**Packages:** `tenacity>=8.3`

**Retry schedule:** 2s → 4s → 8s → 16s → 60s (capped). After 5 failures, log a `webhook_dead_letter` event with the payload for manual recovery.

---

## 7. OpenTelemetry Distributed Tracing

**Why:** Once you have multiple services (API, n8n, Postgres, worker), a single user action spawns spans across all of them. OpenTelemetry exports traces to Jaeger, Grafana Tempo, or Honeycomb so you can see the full call tree in one view.

**Approach:**

```python
# api/telemetry.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

def configure_tracing(app) -> None:
    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")))
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    Psycopg2Instrumentor().instrument()
```

**Packages:** `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-httpx`, `opentelemetry-instrumentation-psycopg2`

**docker-compose addition:** Add a `grafana/otel-lgtm` all-in-one collector service (Loki + Grafana + Tempo + Prometheus in a single container) for local development.

---

## 8. Token Cost Tracking Middleware

**Why:** LLM costs are invisible until the OpenAI invoice arrives. Tracking tokens per agent call lets you build a cost dashboard, set per-caller budgets, and spot unexpectedly expensive queries early.

**Approach:**

```python
# api/middleware/cost_tracker.py
from langchain.callbacks.base import BaseCallbackHandler
import structlog

log = structlog.get_logger()

class CostTrackingCallback(BaseCallbackHandler):
    def on_llm_end(self, response, **kwargs):
        usage = response.llm_output.get("token_usage", {})
        log.info(
            "llm_cost",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            model=response.llm_output.get("model_name", "unknown"),
            # estimated_usd can be computed from model pricing table
        )
```

Register via `ChatOpenAI(callbacks=[CostTrackingCallback()])`. Aggregate into `agent_metrics` table for the `/metrics` endpoint so the dashboard can show daily spend.

---

## 9. Graceful API Key Rotation Detection

**Why:** When `OPENAI_API_KEY` expires or is revoked mid-deployment, the API currently crashes on the first LLM call. Detecting expiry at startup (or on `/health`) and returning a clear `degraded` status prevents silent failures.

**Approach (add to `health.py`):**

```python
async def _check_llm_key(client) -> dict:
    try:
        # Minimal probe: list models (< 1ms, no tokens consumed)
        await client.models.alist()
        return {"status": "ok", "detail": "key valid"}
    except openai.AuthenticationError:
        return {"status": "unhealthy", "detail": "API key invalid or expired"}
    except openai.RateLimitError:
        return {"status": "degraded", "detail": "rate limited — key valid but throttled"}
```

Surface this in the health panel already wired into the dashboard.

---

## Priority Matrix

| Enhancement | Impact | Effort | Recommend Next |
|---|---|---|---|
| Rate Limiting | High | Low | ✅ Yes |
| Structured JSON Logging | High | Low | ✅ Yes |
| Request ID Middleware | High | Low | ✅ Yes |
| Async DB Pool | Medium | Medium | After load testing |
| Retry / Backoff | Medium | Low | ✅ Yes |
| Task Queue (arq) | High | Medium | When p99 latency > 5s |
| OpenTelemetry | Medium | Medium | When multi-service |
| Cost Tracking | High | Low | ✅ Yes |
| Key Rotation Detection | Medium | Low | ✅ Yes |

The four "Yes" low-effort items (rate limiting, structured logging, request ID, cost tracking, key rotation) can be added in a single PR and deliver immediate operational value with minimal risk.

---

## Implemented: OpenTelemetry Distributed Tracing (Session 6)

### What was added
- `api/telemetry.py`: OTel SDK init, FastAPI/httpx/psycopg2/Redis auto-instrumentation, `get_tracer()` and `current_trace_id()` helpers, console + OTLP gRPC exporters, no-op fallback when disabled.
- `api/middleware/tracing.py`: `TracingContextMiddleware` bridges OTel `trace_id`/`span_id` into structlog context vars and echoes `X-Trace-ID` response header.
- `api/worker.py`: All 14 arq job functions wrapped in OTel spans; `_request_id` propagated as span attribute connecting HTTP → queue → worker → LangChain.
- `api/main.py`: `configure_tracing(app)` called at startup; per-agent p99 latency tracking via `_latency_windows` (deque, maxlen=100); `/metrics` returns merged `by_agent` dict with p50/p99/samples.
- `dashboard/index.html`: New "LLM Cost & Latency" panel with Chart.js horizontal bar charts for per-agent token cost and p99 latency; 3 summary KPI cards; live polling every 60 s.
- `docker-compose.yml`: `grafana/otel-lgtm` all-in-one service (Grafana on 3001, Jaeger on 16686, Prometheus on 9090, OTLP gRPC on 4317).

### Trace flow
HTTP request → `TracingContextMiddleware` (creates root span, injects trace_id into structlog) → `_enqueue()` passes `_request_id` as arq job arg → arq worker receives `_request_id`, creates child span with same trace context → LangChain callback records token cost + attaches to span → span exported to OTLP collector.

### Grafana / Jaeger access (local dev)
- Grafana: http://localhost:3001 (anonymous admin)
- Jaeger UI: http://localhost:16686
- Prometheus: http://localhost:9090
