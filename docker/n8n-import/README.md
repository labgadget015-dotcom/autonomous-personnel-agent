# n8n Workflow Auto-Import

Place workflow JSON files in this directory to auto-import them when n8n starts for the first time.

n8n supports auto-import via the `N8N_IMPORT_WORKFLOW` env var or the `--import:workflow` CLI flag.
For Docker, copy your exported workflow JSON files here and mount this directory as shown in docker-compose.yml.

## Manual import (recommended for first setup)

1. Start the stack: `docker compose up -d`
2. Open n8n at http://localhost:5678
3. Log in with your `N8N_BASIC_AUTH_USER` / `N8N_BASIC_AUTH_PASSWORD`
4. Go to **Workflows → Import from file**
5. Select `n8n/workflow.json` from this repository

## After import

Set the following environment variables in n8n (**Settings → Variables**):

See `n8n/env-variables.md` for the full list.
All `PERSONNEL_API_URL` and `PERSONNEL_API_TOKEN` variables are pre-injected
via docker-compose environment into the n8n container as `$env.*` variables.
