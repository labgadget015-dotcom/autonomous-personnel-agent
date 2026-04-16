# n8n Environment Variables

Set these in your n8n instance: **Settings → Variables** (or via `.env` for self-hosted).

| Variable | Description | Example |
|---|---|---|
| `PERSONNEL_API_URL` | Base URL of your FastAPI service | `http://localhost:8000` |
| `PERSONNEL_API_TOKEN` | The API token from `.env.example` | `your-secret-token` |
| `SLACK_APPROVAL_CHANNEL` | Slack channel for approval requests | `#hr-approvals` |
| `SLACK_LEGAL_CHANNEL` | Slack channel for critical escalations | `#legal-urgent` |
| `N8N_BASE_URL` | Public URL of your n8n instance | `https://n8n.yourdomain.com` |
| `TALENT_WORKFLOW_ID` | n8n workflow ID for Talent sub-workflow | `workflow-abc123` |
| `SCHEDULING_WORKFLOW_ID` | n8n workflow ID for Scheduling sub-workflow | `workflow-def456` |
| `ONBOARDING_WORKFLOW_ID` | n8n workflow ID for Onboarding sub-workflow | `workflow-ghi789` |
| `PERFORMANCE_WORKFLOW_ID` | n8n workflow ID for Performance sub-workflow | `workflow-jkl012` |
| `KNOWLEDGE_WORKFLOW_ID` | n8n workflow ID for Knowledge sub-workflow | `workflow-mno345` |

## Credentials to configure in n8n UI

1. **Postgres** — for the audit log `Write Audit Log` node
2. **Slack** — for `Send Slack Approval Request` and `Alert Legal/CHRO` nodes
3. **HTTP Header Auth** — for calls to the FastAPI service (token from `PERSONNEL_API_TOKEN`)

## Sub-workflow pattern

Each specialist sub-workflow follows this pattern:

```
Execute Workflow trigger
      ↓
Extract plan + action_tier from input
      ↓
Call FastAPI sub-agent endpoint (e.g. POST /talent/screen)
      ↓
IF action_tier = auto_execute → execute directly
IF action_tier = needs_approval → return result to parent for approval routing
      ↓
Return result to parent orchestrator
```

Create one workflow per agent. Each receives `plan` (JSON string), `action_tier`, and `task_id` as inputs.
