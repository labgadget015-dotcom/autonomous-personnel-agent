# Autonomous Personnel Agent

> A fully autonomous, multi-agent personnel management system built on n8n, FastAPI, LangChain, and PostgreSQL.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com)
[![n8n](https://img.shields.io/badge/n8n-1.60+-orange.svg)](https://n8n.io)

---

## Overview

An autonomous "Chief of Staff" agent that manages all personnel operations end-to-end — recruiting, scheduling, onboarding, performance tracking, and internal knowledge — with a structured autonomy policy and full audit trail.

```
Webhook / Email / Cron Trigger
         ↓
  ┌──────────────────────┐
  │  GUARDRAILS LAYER    │  ← PII detection, keyword flags, injection blocking
  └──────────────────────┘
         ↓
  ┌──────────────────────┐
  │  ORCHESTRATOR AGENT  │  ← Chief of Staff — classifies and routes events
  └──────────────────────┘
         ↓ (parallel)
  ┌──────┬──────┬──────┬──────┬───────┐
  │Talent│Sched.│Onbrd.│Perf. │Knowl. │  ← Specialist sub-agents
  └──────┴──────┴──────┴──────┴───────┘
         ↓
  Autonomy Routing: auto_execute → needs_approval → blocked
         ↓
  Postgres Audit Log (partitioned, 7-year retention)
```

---

## Architecture

| Layer | Technology | Purpose |
|---|---|---|
| Orchestration | n8n (self-hosted) | Event routing, approvals, cron triggers, sub-workflows |
| Agent service | FastAPI + LangChain | Orchestrator + 5 specialist agents + guardrails |
| LLMs | OpenAI GPT-4.1 / GPT-4.1-mini | Reasoning, classification, drafting |
| Database | PostgreSQL 14+ | People graph, interactions, audit log, pipeline |
| Dashboard | Vanilla JS + Chart.js | Real-time KPIs, charts, approval queue |

---

## Repository Structure

```
.
├── api/
│   ├── main.py            # FastAPI service — 18 REST endpoints
│   ├── agents.py          # Orchestrator + 5 specialist agents (LangChain)
│   ├── guardrails.py      # Two-layer safety: PII detection, action tiers
│   ├── requirements.txt   # Python dependencies
│   └── .env.example       # Environment variable template
│
├── db/
│   └── schema.sql         # Full Postgres schema: 11 tables, 5 views, triggers
│
├── n8n/
│   ├── workflow.json      # Importable n8n orchestrator workflow (19 nodes)
│   └── env-variables.md   # n8n environment variable reference
│
├── dashboard/
│   └── index.html         # Real-time monitoring dashboard (no build needed)
│
└── docs/
    ├── START_HERE.md      # 45-minute setup guide
    └── deployment.md      # Full deployment, security, integrations, troubleshooting
```

---

## Agent Capabilities

### Orchestrator (Chief of Staff)
Routes any personnel event to the correct specialist using structured JSON plans. Applies autonomy tier before any action is taken.

### Talent Agent
- Screens candidates against open roles with fit scores (0–10), red flag detection, and bias checks
- Drafts personalised outreach sequences
- Builds pipeline summaries across all open roles

### Scheduling Agent
- Summarises meetings and extracts commitments + action items
- Identifies cold contacts (priority people with no contact in N days)
- Drafts meeting invitations with agendas

### Onboarding Agent
- Generates customised onboarding checklists per role and tech stack
- Drafts welcome emails with links to tools, repos, and docs
- Produces offboarding plans with access revocation checklists

### Performance Agent
- Weekly "Top People to Watch" brief with risk flags and nudges
- Goal risk assessment per collaborator
- Identifies overloaded / underloaded team members

### Knowledge Agent
- RAG-powered policy and SOP Q&A (FAISS + OpenAI embeddings)
- Generates internal documents: policies, SOPs, handbooks, agreements

---

## Autonomy Policy

Every action is classified into one of three tiers before execution:

| Tier | Behaviour | Examples |
|---|---|---|
| `auto_execute` | Executes immediately, logs to audit | Draft emails, record updates, meeting summaries, reminders |
| `needs_approval` | Sends Slack approval request to you | Outreach emails, candidate shortlists, performance flags |
| `blocked` | Proposes only — never executes | Hiring/firing decisions, pay changes, contract signing |

Tiers are configured in `api/guardrails.py` — no prompt changes needed.

---

## Guardrails

**Layer 1 — Input/Output Checks (deterministic):**
- PII detection: email, phone, NI number, passport, IBAN, salary, DOB
- Critical keyword detection: harassment, fraud, discrimination, whistleblowing, threats
- Prompt injection detection: 15 patterns including SQL injection, jailbreak attempts
- Policy flag detection: working time directive, GDPR, pregnancy rights, flexible working

**Layer 2 — Action Constraints:**
- Per-action allow/deny tier mapping
- Context-level tier overrides from orchestrator
- All blocked actions returned as proposals only

---

## Quick Start

### Prerequisites
- Python 3.11+
- PostgreSQL 14+
- n8n self-hosted (v1.60+)
- OpenAI API key

### 1. Database

```bash
createdb personnel_agent
psql -d personnel_agent -f db/schema.sql
```

### 2. FastAPI Service

```bash
cd api
cp .env.example .env          # fill in OPENAI_API_KEY and API_TOKEN
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 3. n8n Workflow

1. Open n8n → **Workflows → Import from file** → select `n8n/workflow.json`
2. Set environment variables per `n8n/env-variables.md`
3. Configure Postgres and Slack credentials
4. Activate the workflow

### 4. Dashboard

```bash
open dashboard/index.html
# or serve locally
python -m http.server 3000 --directory dashboard
```

Enter your FastAPI URL and API token in the dashboard config panel.

### 5. Test

```bash
curl -X POST http://localhost:5678/webhook/personnel-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "type": "email",
    "content": "Interested in collaborating on your LLM automation projects.",
    "sender": "potential@collaborator.com",
    "metadata": {}
  }'
```

Full guide in [`docs/START_HERE.md`](docs/START_HERE.md).

---

## Database Schema

11 tables covering the full personnel lifecycle:

| Table | Purpose |
|---|---|
| `people` | Central relationship graph — every contact, candidate, collaborator |
| `interactions` | Every touchpoint: email, Slack, calendar, GitHub |
| `tasks` | Action items with autonomy tier and approval tracking |
| `events` | Immutable event timeline per person |
| `roles` | Open positions / collaborator slots |
| `candidate_pipeline` | Funnel tracking per candidate per role |
| `onboarding_plans` | Onboarding checklists with progress tracking |
| `performance_goals` | Goals and deliverables per collaborator |
| `agent_approvals` | Human-in-the-loop approval queue |
| `audit_log` | Immutable append-only log (partitioned, 7-year retention) |
| `knowledge_base` | Full-text indexed internal policies and SOPs |

---

## API Reference

Interactive docs at `http://localhost:8000/docs` when running.

Key endpoints:

```
POST /route                    Orchestrator — route any event
POST /talent/screen            Screen a candidate against a role
POST /talent/outreach          Draft personalised outreach email
POST /scheduling/summarise     Summarise meeting + extract action items
POST /scheduling/followups     Identify cold contacts needing follow-up
POST /onboarding/plan          Generate onboarding plan
POST /performance/brief        Weekly "Top People to Watch" brief
POST /knowledge/answer         Policy Q&A via RAG
POST /guardrails/evaluate      Run full guardrail stack on any text
GET  /health                   Health check
```

---

## Security

- Token-based API auth (`x-api-token` header on all endpoints)
- PII auto-masked before audit logging
- Per-agent least-privilege DB roles recommended (see `docs/deployment.md`)
- Partitioned audit log — append-only, no `DELETE` granted to service user
- All secrets via environment variables — nothing hardcoded

---

## Roadmap

- [ ] LangGraph state machine for complex multi-turn workflows
- [ ] Slack slash command integration (`/personnel screen ...`)
- [ ] Email trigger integration (Gmail / IMAP)
- [ ] GitHub activity integration for performance signals
- [ ] FAISS knowledge base builder script (Notion + Google Drive loaders)
- [ ] Dockerised deployment (compose file)
- [ ] Webhook-based approval callbacks (replace Slack links)

---

## Tech Stack

- [FastAPI](https://fastapi.tiangolo.com) — REST API framework
- [LangChain](https://langchain.com) — Agent orchestration and LLM chains
- [n8n](https://n8n.io) — Workflow automation and trigger management
- [OpenAI](https://openai.com) — GPT-4.1 for reasoning, GPT-4.1-mini for agents
- [PostgreSQL](https://postgresql.org) — Primary database with partitioned audit log
- [FAISS](https://github.com/facebookresearch/faiss) — Vector store for knowledge RAG
- [Chart.js](https://chartjs.org) — Dashboard visualisations

---

## License

MIT — see [LICENSE](LICENSE) for details.
