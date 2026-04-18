# Claude Code — Project Handoff Prompt
## Autonomous Personnel Agent — Session 9

---

## Who you are picking up from

This project has been built over 8 sessions by an AI agent (Perplexity Computer). You are taking over as the primary engineering agent. Everything is committed and pushed. Your job is to continue from here.

---

## Repository

```
https://github.com/labgadget015-dotcom/autonomous-personnel-agent
```

Clone it first, then read this entire document before touching any files.

**Git identity for all commits:**
```bash
git config user.email "labgadget015-dotcom@users.noreply.github.com"
git config user.name "Gadget Lab"
```

**Last commit:** `31d34c3` — feat: async DB layer (asyncpg + SQLAlchemy), Alembic migrations, self-eval unit tests

---

## What this system does

A FastAPI-based autonomous personnel management platform. It exposes 22 endpoints across 6 HR agent domains (talent, scheduling, onboarding, performance, knowledge, guardrails), backed by arq async task queues, Postgres, Redis, LangChain, and a self-refining evaluation loop.

**The system can evaluate its own outputs, generate verbal lessons from failures, version and A/B test its own prompts, and autonomously refine them over time — without fine-tuning.**

---

## Full Architecture (current state)

### Stack
- **FastAPI** (async, Pydantic v2) — `api/main.py`
- **asyncpg + SQLAlchemy async** — `api/db.py` (replaced psycopg2 in Session 8)
- **arq** (Redis-backed async task queue) — `api/worker.py`
- **LangChain + ChatOpenAI** — `api/agents.py`
- **structlog** JSON logging — `api/logging_config.py`
- **slowapi** rate limiting (60 req/min per token, Redis-backed) — `api/middleware/rate_limit.py`
- **OpenTelemetry** (FastAPI + httpx + SQLAlchemy instrumentation) — `api/telemetry.py`
- **grafana/otel-lgtm** (all-in-one: Grafana, Jaeger, Prometheus, Loki) — `docker-compose.yml`
- **Alembic** migrations — `alembic/` at repo root

### Middleware stack (in order)
1. `RequestIDMiddleware` — UUID per request → structlog bind → `X-Request-ID` response header
2. `TracingContextMiddleware` — OTel trace_id/span_id → structlog → `X-Trace-ID` header
3. `slowapi` — per-token rate limiting

### All API Endpoints
**Auth:** `x-api-token` header on all except `/health`, `/health/live`, `/docs`

**Sync agents:**
- `POST /route` — intelligent request routing (Orchestrator)
- `POST /talent/screen`, `/talent/outreach`, `/talent/pipeline-summary`
- `POST /scheduling/summarise`, `/scheduling/followups`, `/scheduling/invite`
- `POST /onboarding/plan`, `/onboarding/check`, `/onboarding/offboarding`
- `POST /performance/brief`, `/performance/goal-risk`
- `POST /knowledge/answer`, `/knowledge/generate`
- `POST /guardrails/evaluate`

**Async mirrors (arq-backed):**
- `POST /async/<all above>` → `{job_id, status: "queued"}`
- `GET /jobs/{job_id}` — poll result

**System:**
- `GET /health` — deep health: Postgres (asyncpg probe), Redis, LLM key check → 200/degraded, 503
- `GET /health/live` — always 200 (k8s liveness)
- `GET /metrics` — token cost + p99/p50 latency per agent

**Self-Refining (tagged `SelfRefining` in OpenAPI):**
- `GET /prompts` — active prompt version + avg score per agent
- `GET /prompts/{agent}` — full version history
- `POST /prompts/{agent}/rollback` — revert to last archived version
- `GET /outcomes` — recent evaluation results (filterable by agent)
- `GET /outcomes/stats` — 24h pass rate + avg score per agent
- `GET /reflections` — active verbal lessons (filterable by agent)
- `DELETE /reflections/{id}` — human curation

### Worker Jobs (arq)
```
run_route, run_talent_screen, run_talent_outreach, run_talent_pipeline_summary,
run_scheduling_summarise, run_scheduling_followups, run_scheduling_invite,
run_onboarding_plan, run_onboarding_check, run_onboarding_offboarding,
run_performance_brief, run_performance_goal_risk,
run_knowledge_answer, run_knowledge_generate,
run_evaluate_outcome          ← evaluation + reflection storage
run_prompt_refiner            ← cron: 00/06/12/18 UTC
```

### Database (Postgres, 8 tables via Alembic)
**Original tables:** `people`, `interactions`, `tasks`, `events`, `roles`, `candidate_pipeline`, `onboarding_plans`, `performance_goals`, `agent_approvals`, `audit_log` (partitioned 2025–2032), `agent_metrics`, `knowledge_base`

**Self-refining tables (revision 0002):**
- `agent_outcomes` — every evaluation result (score, passed, critique, rubric_scores JSONB, prompt_version, trace_id)
- `agent_reflections` — verbal lessons (reflection text, failure_type, applied_count, is_active)
- `prompt_versions` — versioned system prompts per agent (status: active/candidate/archived/rollback)
- `eval_runs` — A/B test records

### Self-Refining System
Four new modules, all **off by default** (feature-flagged):

| File | Role |
|---|---|
| `api/evaluation.py` | LLM-as-judge: scores output 0–10 across quality/completeness/guardrails/task_success |
| `api/reflection.py` | Generates verbal "WHAT WENT WRONG / NEXT TIME" lessons; injects top-3 into future prompts |
| `api/prompt_manager.py` | Versioned prompt store; A/B routing; promote_candidate(); rollback |
| `api/refiner.py` | arq cron: clusters failures → ChatOpenAI generates improved prompt → writes as candidate |

**Trace flow when SELF_EVAL_ENABLED=true:**
```
HTTP request
 → arq job runs agent
 → enqueues run_evaluate_outcome (fire-and-forget, never blocks result)
 → judge scores output → writes agent_outcomes
 → if failed: generate_reflection() → writes agent_reflections
 → next call: top-3 reflections prepended to system prompt
 → every 6h: refiner clusters failures → proposes new prompt candidate
 → A/B tested on 10% traffic → promoted if score improves
```

### Dashboard (gh-pages, live)
URL: `https://labgadget015-dotcom.github.io/autonomous-personnel-agent/`
7 panels: Header | Health Checks | LLM Cost & Latency | Agent Self-Improvement | KPI Grid | Agent Activity | System Metrics

### Infrastructure (docker-compose, 6 services)
`api` (FastAPI, port 8000) | `postgres` | `redis` | `worker` (arq) | `n8n` | `otel-collector` (grafana/otel-lgtm)

Local observability:
- Grafana: http://localhost:3001
- Jaeger: http://localhost:16686
- Prometheus: http://localhost:9090

### CI/CD
- **CI** (`.github/workflows/ci.yml`): ruff lint + n8n JSON validation on every push
- **CD** (`.github/workflows/cd.yml`): Docker build → GHCR push → `alembic upgrade head` → integration tests → all-checks gate on main merge

### Environment Variables (complete)
```env
# Core
API_TOKEN=your-secret-token
OPENAI_API_KEY=sk-...
DATABASE_URL=postgresql://postgres:password@postgres:5432/personnel
REDIS_URL=redis://redis:6379

# OpenTelemetry
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_SERVICE_NAME=autonomous-personnel-agent

# Self-Refining (off by default)
SELF_EVAL_ENABLED=false
PROMPT_REFINE_ENABLED=false
AB_TEST_SPLIT=10
REFINE_MIN_FAILURES=3
EVAL_JUDGE_MODEL=gpt-4.1-mini
REFINE_MODEL=gpt-4.1

# Optional
LOG_LEVEL=INFO
RATE_LIMIT=60/minute
N8N_WEBHOOK_URL=https://gadgetlab.app.n8n.cloud/webhook/...
```

---

## What is NOT yet built (prioritised backlog)

### Priority 1 — Enable SELF_EVAL_ENABLED (no code needed)
Set `SELF_EVAL_ENABLED=true` in `.env` and restart. The system is production-safe. After 1–2 weeks of data, enable `PROMPT_REFINE_ENABLED=true`.

### Priority 2 — Grafana Dashboard Provisioning (~2h)
The `otel-lgtm` collector is running but stakeholders see a blank Grafana canvas.
- Create `grafana/provisioning/dashboards/personnel-agent.json` — pre-built FastAPI RED metrics (request rate, error rate, p99)
- Create `grafana/provisioning/datasources/prometheus.yaml` + `loki.yaml`
- Mount into `otel-collector` container via docker-compose volumes
- Add "Open Grafana →" link in `dashboard/index.html`

### Priority 3 — Budget + Pass-Rate Alerts via n8n (~2h)
- `DAILY_COST_BUDGET_USD` env var (default `5.00`) + `MIN_PASS_RATE_ALERT` (default `70`)
- In `run_evaluate_outcome`: after writing outcome, query cumulative daily spend + per-agent pass rate
- If threshold breached → `post_webhook()` via existing `webhooks.py` (tenacity retry already in place)
- Add budget % to `/metrics` response and dashboard panel

### Priority 4 — Multi-tenant Token Support (~3h)
- Alembic revision 0003: `tokens` table (token_hash, name, tier, last_used_at)
- Update `RequestIDMiddleware` to resolve token → caller name → structlog bind + `agent_outcomes.request_id`
- Update slowapi `key_func` to use caller name
- `POST /admin/tokens` — admin-tier token issuance

### Priority 5 — End-to-End Playwright Tests (~3h)
- Full async job lifecycle: enqueue → poll → result → `GET /outcomes` shows record
- Dashboard UI: health panel green, self-improvement panel loads
- Run in CD pipeline against Docker stack

---

## Critical Gotchas — Read Before Touching Anything

1. **gh-pages update**: NEVER checkout `gh-pages` inside the main clone — it deletes the working tree. Always: `git clone <repo> /tmp/gh-pages-sN && cd /tmp/gh-pages-sN && git checkout gh-pages`

2. **SQLAlchemy text() params**: Use `:param_name` style (not `%s`). Pass as dict: `await session.execute(text("WHERE agent = :agent"), {"agent": "talent"})`

3. **asyncpg health probe**: Use raw `postgresql://` URL directly — asyncpg handles it natively. Do not convert to `postgresql+asyncpg://` for direct asyncpg connections.

4. **Session None handling**: Every function that takes `session: AsyncSession | None` must handle `None` gracefully and return a safe default. Never remove these guards — they protect dev environments without Postgres.

5. **Self-eval is fire-and-forget**: `run_evaluate_outcome` is always enqueued, never awaited. Never change this — it would block agent results.

6. **Feature flags first**: All self-refining code checks `SELF_EVAL_ENABLED` / `PROMPT_REFINE_ENABLED` as the first line and returns safe defaults when false. Never remove flag checks.

7. **Alembic from repo root**: `alembic.ini` is at the repo root (not `api/`). Run `python -m alembic -c alembic.ini upgrade head` from root. New schema changes go in a new Alembic revision (0003, etc.) — never edit existing revisions.

8. **Import paths**: Files in `api/` use `from db import ...` when run from within `api/` directory but may need `from api.db import ...` when imported from repo root. Follow the pattern already established in each file.

9. **DB parameter style change**: All new SQL must use SQLAlchemy `:param` style. The old `%s` psycopg2 style is completely gone.

10. **Dashboard port 3000 is taken by the dashboard service** — Grafana is mapped to `3001:3000`. Do not change this.

11. **Chart.js canvas reuse**: Call `chart.destroy()` before recreating a Chart instance in the same `<canvas>` element.

12. **Large files**: `main.py` ~900 lines, `worker.py` ~700 lines. Use targeted `grep` + line-range reads, not full file reads.

---

## File Map (key files only)

```
api/
  main.py          — all 22 FastAPI endpoints + middleware + lifespan
  db.py            — async DB layer (init_db, get_session, session_ctx)
  agents.py        — LangChain agent implementations
  worker.py        — 15 arq jobs + WorkerSettings + crons
  evaluation.py    — LLM-as-judge (EvalResult, evaluate_output)
  reflection.py    — reflection memory (generate_reflection, get_active_reflections)
  prompt_manager.py — versioned prompts (PromptManager, promote_candidate)
  refiner.py       — background refinement cron (run_prompt_refiner)
  health.py        — /health deep probe (asyncpg + Redis + LLM key)
  telemetry.py     — OTel SDK setup + SQLAlchemy instrumentation
  logging_config.py — structlog JSON config
  webhooks.py      — tenacity webhook retry + dead-letter logging
  middleware/
    request_id.py  — UUID injection
    tracing.py     — OTel trace_id → structlog + X-Trace-ID header
    cost_tracker.py — LangChain CostTrackingCallback
    rate_limit.py  — slowapi limiter
  tests/
    conftest.py        — pytest fixtures (mock_app, db_conn asyncpg)
    test_endpoints.py  — all agent endpoints (mocked LLM)
    test_schema.py     — DB schema assertions
    test_self_refining.py — 21 unit tests (evaluation, reflection, prompt_manager, db)

alembic/
  env.py           — async alembic env
  versions/
    0001_initial_schema.py
    0002_self_refining_tables.py

dashboard/
  index.html       — 7-panel stakeholder dashboard (Chart.js 4.4.0, dark theme)

db/
  schema.sql       — reference SQL (Alembic is authoritative for migrations)

docker-compose.yml — 6 services
.github/workflows/
  ci.yml           — ruff lint + n8n JSON validation
  cd.yml           — build + GHCR + alembic upgrade + integration tests + gate
docs/
  enhancements.md  — full backlog with implementation notes and code examples
```

---

## How to Start

```bash
# 1. Clone
git clone https://github.com/labgadget015-dotcom/autonomous-personnel-agent
cd autonomous-personnel-agent

# 2. Read the key files before touching anything
cat docs/enhancements.md          # full backlog
cat api/main.py                   # understand endpoint + DB patterns
cat api/db.py                     # understand async DB layer
cat api/worker.py                 # understand job + span pattern

# 3. Run locally
cp .env.example .env              # fill in API_TOKEN + OPENAI_API_KEY + DATABASE_URL
docker-compose up                 # starts all 6 services
alembic upgrade head              # apply migrations to local Postgres

# 4. Run tests
cd api && pytest tests/ -v

# 5. Enable self-evaluation
# In .env: SELF_EVAL_ENABLED=true
# Restart: docker-compose restart api worker

# 6. Verify
curl -H "x-api-token: $TOKEN" http://localhost:8000/outcomes/stats
curl -H "x-api-token: $TOKEN" http://localhost:8000/prompts
```

---

## Commit Convention

```
feat: <description>          — new functionality
fix: <description>           — bug fix
chore: <description>         — maintenance (deps, config)
docs: <description>          — docs/dashboard only
test: <description>          — tests only
```

After every set of changes:
1. Commit + push `main`
2. If `dashboard/index.html` changed: update `gh-pages` branch (separate clone)
3. Log work in a `SESSION_LOG_N.md` and upload to Google Drive
4. Update `NEXT_AGENT_BRIEFING_N.md` for the next handoff

---

*Repo: https://github.com/labgadget015-dotcom/autonomous-personnel-agent | Last commit: 31d34c3 | 8 sessions, 22 endpoints, 16 arq jobs, 8 DB tables, 21 unit tests, full self-refining loop*
