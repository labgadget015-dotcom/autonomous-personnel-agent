# Deployment Guide — Autonomous Personnel Agent

## Table of Contents

1. [Architecture Overview](#architecture)
2. [Prerequisites](#prerequisites)
3. [Database Setup](#database)
4. [FastAPI Service](#fastapi)
5. [n8n Configuration](#n8n)
6. [Dashboard](#dashboard)
7. [Integrations](#integrations)
8. [Security](#security)
9. [Monitoring](#monitoring)
10. [Troubleshooting](#troubleshooting)

---

## 1. Architecture Overview {#architecture}

```
┌─────────────────────────────────────────────────┐
│                   TRIGGERS                       │
│  Email  │  Webhook  │  Slack  │  Cron            │
└────────────────┬────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────┐
│              n8n ORCHESTRATOR WORKFLOW           │
│  Guardrails → Route Event → Sub-Workflows        │
│  Approval routing → Audit log → Response         │
└────────────────┬────────────────────────────────┘
                 │ HTTP (POST)
┌────────────────▼────────────────────────────────┐
│              FastAPI SERVICE (port 8000)          │
│  Orchestrator Agent + 5 Specialist Agents        │
│  Guardrails layer                                │
│  LangChain + OpenAI (or Anthropic)              │
└────────────────┬────────────────────────────────┘
                 │
┌────────────────▼────────────────────────────────┐
│              POSTGRES DATABASE                   │
│  11 tables: people, interactions, tasks, events  │
│  roles, pipeline, onboarding, goals, approvals   │
│  audit_log (partitioned), metrics, knowledge_base│
└─────────────────────────────────────────────────┘
```

---

## 2. Prerequisites {#prerequisites}

- **Python** 3.11+
- **Postgres** 14+ (local or Supabase)
- **n8n** self-hosted (v1.60+) or n8n cloud
- **OpenAI API key** (or Anthropic for Claude)
- **Slack bot token** (optional, for approvals)
- **Docker** (optional, for containerised deployment)

---

## 3. Database Setup {#database}

### Option A: Local Postgres

```bash
# Create database
createdb personnel_agent

# Apply schema (all tables, views, triggers, seed data)
psql -d personnel_agent -f db/schema.sql

# Verify tables created
psql -d personnel_agent -c "\dt public.*"

# Check views
psql -d personnel_agent -c "\dv"
```

### Option B: Supabase (recommended for production)

1. Create project at supabase.com
2. Go to SQL Editor
3. Paste contents of `db/schema.sql` and run
4. Copy the connection string (`Settings → Database → Connection string → URI`)
5. Set `DATABASE_URL` in `.env`

### Useful queries once running

```sql
-- View all people in the graph
SELECT name, type, status, priority, last_contact_at FROM people ORDER BY priority DESC;

-- Pending approvals
SELECT * FROM v_pending_approvals;

-- Urgent tasks
SELECT * FROM v_urgent_tasks;

-- Cold relationships needing follow-up
SELECT * FROM mv_cold_relationships;
-- (Refresh manually or schedule: REFRESH MATERIALIZED VIEW CONCURRENTLY mv_cold_relationships)

-- Last 50 agent actions
SELECT agent_name, action, action_tier, confidence, success, created_at
FROM audit_log
ORDER BY created_at DESC
LIMIT 50;

-- Pipeline health per role
SELECT * FROM v_pipeline_health;

-- Agent activity last 30 days
SELECT * FROM v_agent_activity_30d;
```

---

## 4. FastAPI Service {#fastapi}

### Installation

```bash
cd api/

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
```

Edit `.env`:
```env
OPENAI_API_KEY=sk-proj-...
API_TOKEN=generate-a-long-random-string-here
DATABASE_URL=postgresql://user:pass@localhost:5432/personnel_agent
ORCHESTRATOR_MODEL=gpt-4.1
AGENT_MODEL=gpt-4.1-mini
PORT=8000
ENV=production
```

### Running

```bash
# Development (hot reload)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2

# With gunicorn (recommended for production)
gunicorn main:app -w 2 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

### Docker

```dockerfile
# Dockerfile (create in api/ directory)
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t personnel-agent-api ./api
docker run -d --env-file api/.env -p 8000:8000 personnel-agent-api
```

### Verify endpoints

```bash
# Health check (no auth needed)
curl http://localhost:8000/health

# API docs (interactive Swagger UI)
open http://localhost:8000/docs

# Test orchestrator
curl -X POST http://localhost:8000/route \
  -H "x-api-token: your-token" \
  -H "Content-Type: application/json" \
  -d '{"type":"manual","content":"I need to onboard a new developer","sender":"me@example.com","metadata":{}}'
```

---

## 5. n8n Configuration {#n8n}

### Import workflow

1. Open n8n → **Workflows → Import from file**
2. Select `n8n/workflow.json`
3. The workflow will appear with 19 pre-built nodes

### Set environment variables

In n8n: **Settings → Variables** (for n8n Cloud / self-hosted with env support):

```
PERSONNEL_API_URL     = http://localhost:8000
PERSONNEL_API_TOKEN   = (same as API_TOKEN in .env)
SLACK_APPROVAL_CHANNEL = #hr-approvals
SLACK_LEGAL_CHANNEL   = #legal-urgent
N8N_BASE_URL          = https://your-n8n-domain.com
```

For workflow IDs (TALENT_WORKFLOW_ID etc.): create placeholder sub-workflows first, then update.

### Configure credentials

In n8n → **Credentials**:

1. **Postgres** — `PostgreSQL` credential type, point to your database
2. **Slack** — `Slack OAuth2 API` or `Slack Bot Token`
3. **HTTP Header Auth** — name: `x-api-token`, value: your API token

Assign each credential to the relevant nodes in the workflow.

### Create sub-workflows

Each specialist agent needs its own n8n sub-workflow. Create one per agent using this template:

```
Execute Workflow Trigger
      ↓
Code node: extract plan + action_tier from $json
      ↓
HTTP Request: POST to relevant FastAPI endpoint
  e.g. POST /talent/screen with candidate + role payload
      ↓
Code node: format result
      ↓
Return result to parent workflow
```

Once created, copy the workflow ID from the URL and set it as the relevant env variable (e.g. `TALENT_WORKFLOW_ID`).

### Activate

1. Click the **Activate** toggle (top right of workflow editor)
2. Your webhook is now live at: `https://your-n8n.com/webhook/personnel-webhook`

### Test

```bash
curl -X POST https://your-n8n.com/webhook/personnel-webhook \
  -H "Content-Type: application/json" \
  -d '{"type":"email","content":"New collaborator enquiry","sender":"test@example.com","metadata":{}}'
```

---

## 6. Dashboard {#dashboard}

```bash
# Option 1: open directly in browser
open dashboard/index.html

# Option 2: serve locally
python -m http.server 3000 --directory dashboard

# Option 3: serve with nginx (production)
# Add a location block pointing to the dashboard/ directory
```

In the dashboard:
1. Click **Dashboard Configuration** (dropdown)
2. Enter your FastAPI URL and API token
3. Click **Connect & Refresh**

Enable **Auto-refresh** for live monitoring (refreshes every 30 seconds).

---

## 7. Integrations {#integrations}

### Gmail / Email (as event source)

In n8n: add a **Gmail Trigger** or **IMAP Trigger** node before "Normalise Input".
Set it to trigger on new emails to a dedicated HR inbox.

### GitHub (for performance signals)

Add a Postgres node to your Performance sub-workflow that queries GitHub's API for:
- Commits per person per week
- Open PRs / review requests
- Issue assignments

Pass this as `github_activity` to the `/performance/brief` endpoint.

### Slack (as event source + approval channel)

1. Create a Slack App with bot scopes: `chat:write`, `channels:read`, `commands`
2. Add a Slack Slash Command `/personnel` pointing to your n8n webhook
3. Configure the Slack credential in n8n
4. Test: `/personnel screen candidate@example.com for LLM Engineer role`

### Notion / Google Docs (knowledge base)

To feed documents into the Knowledge Agent's RAG:

```python
# scripts/index_docs.py — run once to build the FAISS index
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders import NotionDirectoryLoader  # or GDriveLoader

loader = NotionDirectoryLoader("path/to/notion-export")
docs = loader.load()

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vectorstore = FAISS.from_documents(docs, embeddings)
vectorstore.save_local("knowledge_index")

# Then set KNOWLEDGE_VECTORSTORE_PATH=./knowledge_index in .env
```

---

## 8. Security {#security}

### API Token

Generate a strong token:
```bash
openssl rand -hex 32
```
Set as `API_TOKEN` in `.env` and `PERSONNEL_API_TOKEN` in n8n variables.

### Network

- Run FastAPI behind a reverse proxy (nginx/Caddy)
- Use HTTPS in production
- Restrict FastAPI to internal network; only expose via n8n webhook
- Set `CORS_ORIGINS` to specific domains (not `*`) in production

### Postgres

- Create a dedicated `personnel_agent` Postgres user with limited permissions:
```sql
CREATE USER personnel_agent_user WITH PASSWORD 'strong-password';
GRANT CONNECT ON DATABASE personnel_agent TO personnel_agent_user;
GRANT USAGE ON SCHEMA public TO personnel_agent_user;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO personnel_agent_user;
GRANT SELECT, USAGE ON ALL SEQUENCES IN SCHEMA public TO personnel_agent_user;
-- Deny DROP/TRUNCATE/DELETE on audit_log for immutability
REVOKE DELETE ON audit_log FROM personnel_agent_user;
```

### Secrets

- Never hardcode secrets in code
- Use n8n's built-in credential vault for all API keys
- Use Docker secrets or a secret manager (AWS SSM, Vault) for production

### PII in logs

The guardrails layer masks PII before logging. Ensure:
- `sanitised_text` is used in audit log (not raw text)
- Postgres database is encrypted at rest (Supabase handles this; for local, use `pgcrypto`)
- Audit log partitions older than 7 years are dropped on schedule

---

## 9. Monitoring {#monitoring}

### Built-in dashboard

The `dashboard/index.html` provides real-time KPI cards, charts, and an audit table.

### Postgres monitoring queries

```sql
-- Agents with high error rates this week
SELECT agent_name, COUNT(*) FILTER (WHERE success = FALSE) AS errors
FROM audit_log
WHERE created_at >= NOW() - INTERVAL '7 days'
GROUP BY agent_name
HAVING COUNT(*) FILTER (WHERE success = FALSE) > 0
ORDER BY errors DESC;

-- Average agent confidence dropping (possible model drift)
SELECT
  agent_name,
  DATE_TRUNC('day', created_at) AS day,
  ROUND(AVG(confidence)::NUMERIC, 3) AS avg_confidence,
  COUNT(*) AS actions
FROM audit_log
WHERE created_at >= NOW() - INTERVAL '14 days'
  AND confidence IS NOT NULL
GROUP BY agent_name, day
ORDER BY agent_name, day DESC;

-- Approval bottlenecks (approvals pending >24h)
SELECT request_id, agent_name, action_type, created_at,
  EXTRACT(EPOCH FROM (NOW() - created_at))/3600 AS hours_pending
FROM agent_approvals
WHERE status = 'pending'
  AND created_at < NOW() - INTERVAL '24 hours'
ORDER BY created_at ASC;
```

### Alerts (n8n cron)

Create a monitoring workflow in n8n that runs every hour:
1. Query `audit_log` for error count > threshold → Slack alert
2. Query `agent_approvals` for items pending > 48h → Slack reminder
3. Query `performance_goals` for at-risk goals → Slack digest

---

## 10. Troubleshooting {#troubleshooting}

| Issue | Likely cause | Fix |
|---|---|---|
| `FastAPI 401 Unauthorized` | Wrong API token | Check `API_TOKEN` in `.env` matches `PERSONNEL_API_TOKEN` in n8n |
| `FastAPI 500` on agent calls | Missing `OPENAI_API_KEY` or rate limit | Check `.env`; add retry logic |
| n8n webhook not firing | Workflow not activated | Click Activate in n8n editor |
| Postgres `connection refused` | DB not running or wrong URL | `sudo systemctl status postgresql`; verify `DATABASE_URL` |
| Council agents taking >15s | Model latency or token limit | Reduce `max_tokens`; switch to `gpt-4.1-mini` for sub-agents |
| Dashboard shows no data | API not connected | Enter URL + token in dashboard config, click Connect |
| PII showing in audit log | Sanitised text not used | Ensure you log `sanitised_text` from guardrail result, not raw input |
| n8n sub-workflow not found | Wrong workflow ID in env vars | Check workflow IDs in n8n URL bar; update env variables |

### Debug mode

```bash
# Run FastAPI with debug logging
ENV=development uvicorn main:app --host 0.0.0.0 --port 8000 --reload --log-level debug

# Test guardrails standalone
cd api && python guardrails.py

# Test agents standalone (requires OPENAI_API_KEY in env)
python -c "
from agents import TalentAgent
agent = TalentAgent()
result = agent.screen_candidate(
    {'name': 'Test Dev', 'skills': ['python', 'n8n', 'LLMs'], 'experience': '5 years'},
    {'title': 'LLM Engineer', 'required_skills': ['python', 'LLMs']}
)
import json; print(json.dumps(result, indent=2))
"
```
