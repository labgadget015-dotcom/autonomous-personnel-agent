# Autonomous Personnel Agent — START HERE

> Read this in 5 minutes. Then follow the 45-minute setup below.

---

## What You Have

A production-ready, multi-agent personnel management system built for your n8n + Postgres + Python stack.

```
Webhook / Email / Cron
        ↓
GUARDRAILS LAYER (deterministic: PII, keywords, injection)
        ↓
ORCHESTRATOR AGENT (Chief of Staff — classifies + routes)
        ↓
SPECIALIST SUB-AGENTS (parallel, single-responsibility)
├── TalentAgent      — sourcing, screening, outreach drafts
├── SchedulingAgent  — meetings, follow-ups, cold contacts
├── OnboardingAgent  — checklists, access, welcome emails
├── PerformanceAgent — goals, weekly brief, risk nudges
└── KnowledgeAgent   — RAG over your policies and SOPs
        ↓
AUTONOMY ROUTING
├── auto_execute   → runs immediately, logs to audit
├── needs_approval → Slack approval message to you
└── blocked        → proposal only, never executes
        ↓
AUDIT LOG (Postgres, partitioned, 7-year retention)
```

---

## File Structure

```
personnel-agent/
├── api/
│   ├── main.py          — FastAPI service (all agent endpoints)
│   ├── agents.py        — Orchestrator + 5 specialist agents
│   ├── guardrails.py    — PII detection, keyword checks, action tiers
│   ├── requirements.txt — Python dependencies
│   └── .env.example     — Environment variable template
├── db/
│   └── schema.sql       — Complete Postgres schema (11 tables, views, triggers)
├── n8n/
│   ├── workflow.json    — Importable n8n orchestrator workflow (19 nodes)
│   └── env-variables.md — n8n environment variable reference
├── dashboard/
│   └── index.html       — Real-time monitoring dashboard (no build needed)
└── docs/
    ├── START_HERE.md    — This file
    └── deployment.md    — Full deployment guide
```

---

## 45-Minute Quick Start

### Step 1 — Database (10 min)

```bash
# Create the database
createdb personnel_agent

# Run schema (creates 11 tables + views + triggers)
psql -d personnel_agent -f db/schema.sql

# Verify
psql -d personnel_agent -c "\dt"
```

### Step 2 — FastAPI Service (10 min)

```bash
cd api/

# Copy and fill in environment variables
cp .env.example .env
# Edit .env: set OPENAI_API_KEY, API_TOKEN, DATABASE_URL

# Install dependencies (Python 3.11+ recommended)
pip install -r requirements.txt

# Start the service
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Verify
curl http://localhost:8000/health
```

### Step 3 — n8n Workflow (10 min)

1. Open your n8n instance
2. Go to **Workflows → Import**
3. Select `n8n/workflow.json`
4. Go to **Settings → Variables** and add the variables from `n8n/env-variables.md`
5. Configure credentials: **Postgres**, **Slack**, and your FastAPI **HTTP header auth**
6. **Activate** the workflow

### Step 4 — Dashboard (2 min)

```bash
# Just open the file directly in your browser
open dashboard/index.html

# Or serve it
python -m http.server 3000 --directory dashboard/
```

Enter your FastAPI URL and API token in the dashboard config section.

### Step 5 — First Test (10 min)

```bash
# Test the webhook (replace with your n8n URL)
curl -X POST http://localhost:5678/webhook/personnel-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "type": "email",
    "content": "Hi, I saw your GitHub projects and would love to discuss a potential collaboration on your n8n automation work.",
    "sender": "newcontact@example.com",
    "metadata": {}
  }'

# Test the orchestrator directly
curl -X POST http://localhost:8000/route \
  -H "x-api-token: your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "manual",
    "content": "Screen this candidate for the Senior LLM Engineer role",
    "sender": "you@yourdomain.com",
    "metadata": {"candidate_email": "candidate@example.com"}
  }'

# Test guardrails
curl -X POST http://localhost:8000/guardrails/evaluate \
  -H "x-api-token: your-token" \
  -H "Content-Type: application/json" \
  -d '{"text": "My manager has been harassing me about my flexible working request."}'
```

### Step 6 — Customise (remaining time)

Edit `api/guardrails.py`:
```python
# Add your own critical keywords
CRITICAL_KEYWORDS = [..., "bribery", "competitor NDA"]

# Adjust action tiers for your workflow
DEFAULT_ACTION_TIERS["send_outreach_email"] = ActionTier.AUTO_EXECUTE  # if you trust it
```

Edit `api/agents.py` system prompts to fit your tone, industry, and working style.

---

## Autonomy Tiers Explained

| Tier | What happens | Examples |
|---|---|---|
| `auto_execute` | Agent acts immediately, logs to audit | Draft emails, record updates, summaries, reminders |
| `needs_approval` | Sends you a Slack message with Approve/Reject | Outreach emails, candidate shortlists, performance flags |
| `blocked` | Agent proposes but cannot execute | Hiring decisions, pay changes, contract signing |

You control tiers in `DEFAULT_ACTION_TIERS` in `guardrails.py` — no model changes needed.

---

## Real Usage Examples

### Someone emails you out of the blue
1. n8n email trigger fires → webhook payload normalised
2. Guardrails: PII check passes, no critical keywords
3. Orchestrator: `task_type = "relationship"`, `action_tier = "auto_execute"`
4. SchedulingAgent: Creates contact record, tags them, drafts a warm reply
5. Audit log entry written
6. Dashboard updates

### You define a new open role
1. POST to `/talent/screen` or add to `roles` table
2. TalentAgent searches your network, GitHub, and email history
3. Returns ranked shortlist (needs_approval tier)
4. You get Slack message: "4 candidates shortlisted for LLM Engineer — approve outreach?"
5. Approve → outreach emails sent automatically

### New hire starts
1. Set person status to `hired` via API or n8n manual trigger
2. OnboardingAgent generates customised checklist
3. Welcome email drafted (needs_approval) → you approve → sent
4. Access tasks created in your PM tool automatically
5. 30-day check-in scheduled

### Weekly Monday morning
1. Cron fires at 8am
2. PerformanceAgent reads all active goals, GitHub commits, interaction history
3. Produces "Top 5 People to Watch" brief sent to your Slack
4. SchedulingAgent identifies cold contacts from `mv_cold_relationships` view
5. Follow-up drafts prepared (auto_execute for low-priority, needs_approval for high-priority)

---

## Common Customisations

```python
# Lower the cold-contact threshold from 14 days to 7
result = agent.identify_cold_followups(people, threshold_days=7)

# Add your company's tools to the default onboarding checklist
# Edit the OnboardingAgent system prompt in agents.py

# Change which model the orchestrator uses
# Set ORCHESTRATOR_MODEL=gpt-4.1 (default) or claude-opus-4 in .env

# Add a custom policy to the knowledge base
psql -d personnel_agent -c "
INSERT INTO knowledge_base (title, category, content, tags)
VALUES ('Remote Work Policy v2', 'policy', 'All contractors may work remotely...', '[\"remote\",\"policy\"]');
"
```

---

## FAQ

**Q: Will it actually send emails without asking me?**
A: Only if you set `send_outreach_email` to `auto_execute` in `DEFAULT_ACTION_TIERS`. By default it's `needs_approval`.

**Q: What LLM does it use?**
A: Orchestrator defaults to `gpt-4.1`. Sub-agents use `gpt-4.1-mini` (cheaper). Both configurable via `.env`.

**Q: Can I use Claude instead of OpenAI?**
A: Yes — change `ChatOpenAI` to `ChatAnthropic` in `agents.py` and install `langchain-anthropic`.

**Q: Does it work with my existing n8n instance?**
A: Yes — import the workflow JSON, point it at your FastAPI service URL, and configure credentials.

**Q: How do I add my own policy documents for the Knowledge Agent?**
A: Use `POST /knowledge/generate` to generate documents, then add to the `knowledge_base` table. For RAG, build a FAISS index and set `KNOWLEDGE_VECTORSTORE_PATH` in `.env`.

**Q: What if an agent makes a mistake?**
A: Every action is logged in the audit table. For `needs_approval` actions, nothing external happens until you confirm. For `auto_execute` actions, you can review the audit log and manually reverse.
