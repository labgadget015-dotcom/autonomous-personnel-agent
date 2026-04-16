-- ============================================================
-- AUTONOMOUS PERSONNEL AI AGENT — POSTGRESQL SCHEMA
-- ============================================================
-- Version: 1.0
-- Description: Complete schema for multi-agent personnel management system
-- Includes: People graph, interactions, tasks, events, roles, audit log,
--           analytics views, triggers, and retention policies

-- ============================================================
-- EXTENSIONS
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";      -- fuzzy search on names/emails
CREATE EXTENSION IF NOT EXISTS "btree_gin";    -- GIN index support for JSONB

-- ============================================================
-- ENUMS
-- ============================================================

CREATE TYPE person_type AS ENUM (
    'lead',
    'candidate',
    'collaborator',
    'client',
    'partner',
    'contractor',
    'employee',
    'alumni',
    'vendor'
);

CREATE TYPE person_status AS ENUM (
    'prospect',
    'contacted',
    'active',
    'onboarding',
    'offboarding',
    'inactive',
    'archived',
    'rejected',
    'hired'
);

CREATE TYPE interaction_channel AS ENUM (
    'email',
    'slack',
    'discord',
    'github',
    'calendar',
    'phone',
    'video',
    'in_person',
    'notion',
    'form',
    'linkedin',
    'other'
);

CREATE TYPE interaction_direction AS ENUM ('inbound', 'outbound', 'internal');

CREATE TYPE task_status AS ENUM (
    'pending',
    'in_progress',
    'blocked',
    'waiting_approval',
    'done',
    'cancelled'
);

CREATE TYPE task_priority AS ENUM ('low', 'medium', 'high', 'urgent');

CREATE TYPE action_tier AS ENUM (
    'auto_execute',      -- No human needed
    'needs_approval',    -- Human must approve before execution
    'blocked'            -- Cannot execute; propose only
);

CREATE TYPE audit_action AS ENUM (
    'create', 'update', 'delete', 'read',
    'email_sent', 'email_draft', 'task_created', 'task_updated',
    'approval_requested', 'approval_granted', 'approval_rejected',
    'escalation_triggered', 'workflow_started', 'workflow_completed',
    'agent_decision', 'guardrail_triggered'
);

CREATE TYPE event_type AS ENUM (
    'applied', 'screened', 'interviewed', 'shortlisted', 'offered',
    'hired', 'rejected', 'withdrew', 'onboarded', 'milestone_reached',
    'performance_review', 'goal_set', 'goal_achieved', 'goal_missed',
    'offboarding_started', 'offboarding_complete', 'relationship_started',
    'relationship_ended', 'contract_signed', 'contract_renewed',
    'complaint_filed', 'note_added', 'tag_changed', 'status_changed'
);

CREATE TYPE escalation_path AS ENUM (
    'auto_log',
    'hrbp_review',
    'hr_lead',
    'legal_urgent'
);

-- ============================================================
-- CORE TABLE: PEOPLE
-- Central node of the relationship graph
-- ============================================================
CREATE TABLE people (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    email           TEXT UNIQUE,
    phone           TEXT,
    role            TEXT,                       -- job title / function
    company         TEXT,
    type            person_type NOT NULL DEFAULT 'lead',
    status          person_status NOT NULL DEFAULT 'prospect',
    tags            JSONB NOT NULL DEFAULT '[]',  -- ["LLM", "remote", "python"]
    source          TEXT,                         -- where first encountered
    owner_email     TEXT,                         -- which human owns this relationship
    linkedin_url    TEXT,
    github_username TEXT,
    timezone        TEXT DEFAULT 'UTC',
    country         TEXT,
    language        TEXT DEFAULT 'en',
    priority        INTEGER DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    last_contact_at TIMESTAMPTZ,
    next_followup_at TIMESTAMPTZ,
    notes           TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',  -- arbitrary extra fields
    is_deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_people_email       ON people (email);
CREATE INDEX idx_people_type        ON people (type);
CREATE INDEX idx_people_status      ON people (status);
CREATE INDEX idx_people_tags        ON people USING GIN (tags);
CREATE INDEX idx_people_metadata    ON people USING GIN (metadata);
CREATE INDEX idx_people_name_trgm   ON people USING GIN (name gin_trgm_ops);
CREATE INDEX idx_people_next_followup ON people (next_followup_at) WHERE next_followup_at IS NOT NULL;
CREATE INDEX idx_people_last_contact  ON people (last_contact_at DESC);

-- ============================================================
-- TABLE: INTERACTIONS
-- Every contact logged (email, meeting, slack, etc.)
-- ============================================================
CREATE TABLE interactions (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id     UUID NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    channel       interaction_channel NOT NULL,
    direction     interaction_direction NOT NULL,
    timestamp     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    subject       TEXT,
    summary       TEXT,                    -- AI-generated summary
    sentiment     TEXT,                   -- 'positive', 'neutral', 'negative', 'urgent'
    sentiment_score FLOAT CHECK (sentiment_score BETWEEN -1 AND 1),
    thread_id     TEXT,                   -- email thread ID, Slack thread_ts, etc.
    raw_content   TEXT,                   -- original content (may be hashed/encrypted)
    key_points    JSONB DEFAULT '[]',     -- AI-extracted commitments / next steps
    agent_id      UUID,                   -- which agent processed this
    metadata      JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_interactions_person   ON interactions (person_id);
CREATE INDEX idx_interactions_channel  ON interactions (channel);
CREATE INDEX idx_interactions_time     ON interactions (timestamp DESC);
CREATE INDEX idx_interactions_thread   ON interactions (thread_id);

-- ============================================================
-- TABLE: TASKS
-- Action items created by agents or humans, tracked to completion
-- ============================================================
CREATE TABLE tasks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id       UUID REFERENCES people (id) ON DELETE SET NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    status          task_status NOT NULL DEFAULT 'pending',
    priority        task_priority NOT NULL DEFAULT 'medium',
    due_at          TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    assigned_to     TEXT,                  -- human or agent slug
    source_agent    TEXT,                  -- which agent created this
    action_tier     action_tier NOT NULL DEFAULT 'auto_execute',
    approval_by     TEXT,                  -- who approved (if needed)
    approved_at     TIMESTAMPTZ,
    parent_task_id  UUID REFERENCES tasks (id),
    tags            JSONB DEFAULT '[]',
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_tasks_person      ON tasks (person_id);
CREATE INDEX idx_tasks_status      ON tasks (status);
CREATE INDEX idx_tasks_due         ON tasks (due_at) WHERE due_at IS NOT NULL;
CREATE INDEX idx_tasks_priority    ON tasks (priority);
CREATE INDEX idx_tasks_tier        ON tasks (action_tier);
CREATE INDEX idx_tasks_assigned    ON tasks (assigned_to);

-- ============================================================
-- TABLE: EVENTS
-- Immutable event log of what happened to each person
-- ============================================================
CREATE TABLE events (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id   UUID NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    event_type  event_type NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}',  -- role, stage, context
    triggered_by TEXT,                        -- 'agent:talent' or 'human:alice@acme.com'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_events_person   ON events (person_id);
CREATE INDEX idx_events_type     ON events (event_type);
CREATE INDEX idx_events_time     ON events (occurred_at DESC);

-- ============================================================
-- TABLE: ROLES
-- Open positions or collaborator slots you want to fill
-- ============================================================
CREATE TABLE roles (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title           TEXT NOT NULL,
    description     TEXT,
    required_skills JSONB DEFAULT '[]',        -- ["python", "LLMs", "n8n"]
    nice_to_have    JSONB DEFAULT '[]',
    budget_min      NUMERIC(12,2),
    budget_max      NUMERIC(12,2),
    currency        TEXT DEFAULT 'GBP',
    role_type       TEXT DEFAULT 'contractor', -- 'employee', 'contractor', 'volunteer'
    urgency         task_priority DEFAULT 'medium',
    status          TEXT DEFAULT 'open',       -- 'open', 'paused', 'closed', 'filled'
    remote          BOOLEAN DEFAULT TRUE,
    location        TEXT,
    hiring_manager  TEXT,
    outreach_template TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_roles_status ON roles (status);
CREATE INDEX idx_roles_urgency ON roles (urgency);
CREATE INDEX idx_roles_skills ON roles USING GIN (required_skills);

-- ============================================================
-- TABLE: CANDIDATE_PIPELINE
-- Tracks candidates through a funnel for a specific role
-- ============================================================
CREATE TABLE candidate_pipeline (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id       UUID NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    role_id         UUID NOT NULL REFERENCES roles (id) ON DELETE CASCADE,
    stage           TEXT NOT NULL DEFAULT 'sourced',  -- sourced→outreach→responded→screened→interview→offer→hired/rejected
    fit_score       FLOAT CHECK (fit_score BETWEEN 0 AND 10),
    fit_reasons     JSONB DEFAULT '[]',
    red_flags       JSONB DEFAULT '[]',
    agent_recommendation TEXT,           -- 'advance', 'hold', 'reject'
    human_decision  TEXT,                -- overrides agent
    outreach_sent_at TIMESTAMPTZ,
    response_received_at TIMESTAMPTZ,
    interview_at    TIMESTAMPTZ,
    notes           TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (person_id, role_id)
);

CREATE INDEX idx_pipeline_person ON candidate_pipeline (person_id);
CREATE INDEX idx_pipeline_role   ON candidate_pipeline (role_id);
CREATE INDEX idx_pipeline_stage  ON candidate_pipeline (stage);
CREATE INDEX idx_pipeline_score  ON candidate_pipeline (fit_score DESC);

-- ============================================================
-- TABLE: ONBOARDING_PLANS
-- Generated per collaborator/hire, tracks checklist completion
-- ============================================================
CREATE TABLE onboarding_plans (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id       UUID NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    role_id         UUID REFERENCES roles (id),
    start_date      DATE NOT NULL,
    target_complete DATE,
    status          TEXT DEFAULT 'not_started',  -- 'not_started', 'in_progress', 'complete'
    checklist       JSONB NOT NULL DEFAULT '[]', -- [{item, done, due, owner}]
    welcome_sent    BOOLEAN DEFAULT FALSE,
    access_provisioned BOOLEAN DEFAULT FALSE,
    intro_calls_scheduled BOOLEAN DEFAULT FALSE,
    docs_shared     BOOLEAN DEFAULT FALSE,
    notes           TEXT,
    created_by      TEXT,       -- 'agent:onboarding' or 'human:...'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_onboarding_person ON onboarding_plans (person_id);
CREATE INDEX idx_onboarding_status ON onboarding_plans (status);

-- ============================================================
-- TABLE: PERFORMANCE_GOALS
-- Goals and deliverables per person, tracked by Performance Agent
-- ============================================================
CREATE TABLE performance_goals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id       UUID NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT,
    metric          TEXT,                   -- "deliver 3 modules per sprint"
    target_value    TEXT,
    current_value   TEXT,
    status          TEXT DEFAULT 'active',  -- 'active', 'achieved', 'missed', 'cancelled'
    due_at          TIMESTAMPTZ,
    achieved_at     TIMESTAMPTZ,
    risk_flag       BOOLEAN DEFAULT FALSE,
    risk_reason     TEXT,
    nudge_sent_at   TIMESTAMPTZ,
    agent_assessment JSONB DEFAULT '{}',
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_goals_person  ON performance_goals (person_id);
CREATE INDEX idx_goals_status  ON performance_goals (status);
CREATE INDEX idx_goals_due     ON performance_goals (due_at);
CREATE INDEX idx_goals_risk    ON performance_goals (risk_flag) WHERE risk_flag = TRUE;

-- ============================================================
-- TABLE: AGENT_APPROVALS
-- Human-in-the-loop approval queue for escalated decisions
-- ============================================================
CREATE TABLE agent_approvals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    request_id      TEXT UNIQUE NOT NULL,   -- idempotency key from agent
    agent_name      TEXT NOT NULL,          -- 'talent', 'scheduling', 'onboarding', etc.
    action_type     TEXT NOT NULL,          -- 'send_outreach', 'schedule_interview', etc.
    action_tier     action_tier NOT NULL,
    payload         JSONB NOT NULL,         -- full proposed action
    reasoning       TEXT,                   -- agent's reasoning
    person_id       UUID REFERENCES people (id),
    status          TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected', 'expired'
    reviewed_by     TEXT,                   -- human who responded
    reviewed_at     TIMESTAMPTZ,
    feedback        TEXT,                   -- human's notes on decision
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '48 hours'),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_approvals_status  ON agent_approvals (status);
CREATE INDEX idx_approvals_agent   ON agent_approvals (agent_name);
CREATE INDEX idx_approvals_expires ON agent_approvals (expires_at) WHERE status = 'pending';

-- ============================================================
-- TABLE: AUDIT_LOG
-- Immutable log of all agent and human actions (7-year retention)
-- ============================================================
CREATE TABLE audit_log (
    id            BIGSERIAL PRIMARY KEY,
    log_id        UUID NOT NULL DEFAULT uuid_generate_v4(),
    agent_name    TEXT NOT NULL,                    -- 'orchestrator', 'talent', 'human'
    action        audit_action NOT NULL,
    person_id     UUID,
    task_id       UUID,
    approval_id   UUID,
    input_data    JSONB,                            -- sanitised (no raw PII)
    output_data   JSONB,
    reasoning     TEXT,                            -- agent's chain-of-thought
    action_tier   action_tier,
    confidence    FLOAT CHECK (confidence BETWEEN 0 AND 1),
    duration_ms   INTEGER,
    success       BOOLEAN NOT NULL DEFAULT TRUE,
    error_message TEXT,
    ip_address    INET,
    session_id    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
) PARTITION BY RANGE (created_at);

-- Yearly partitions (add more as needed)
CREATE TABLE audit_log_2025 PARTITION OF audit_log
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE audit_log_2026 PARTITION OF audit_log
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE audit_log_2027 PARTITION OF audit_log
    FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');
CREATE TABLE audit_log_2028 PARTITION OF audit_log
    FOR VALUES FROM ('2028-01-01') TO ('2029-01-01');
CREATE TABLE audit_log_2029 PARTITION OF audit_log
    FOR VALUES FROM ('2029-01-01') TO ('2030-01-01');
CREATE TABLE audit_log_2030 PARTITION OF audit_log
    FOR VALUES FROM ('2030-01-01') TO ('2031-01-01');
CREATE TABLE audit_log_2031 PARTITION OF audit_log
    FOR VALUES FROM ('2031-01-01') TO ('2032-01-01');
CREATE TABLE audit_log_2032 PARTITION OF audit_log
    FOR VALUES FROM ('2032-01-01') TO ('2033-01-01');

CREATE INDEX idx_audit_agent    ON audit_log (agent_name);
CREATE INDEX idx_audit_action   ON audit_log (action);
CREATE INDEX idx_audit_person   ON audit_log (person_id);
CREATE INDEX idx_audit_time     ON audit_log (created_at DESC);
CREATE INDEX idx_audit_success  ON audit_log (success) WHERE success = FALSE;

-- ============================================================
-- TABLE: AGENT_METRICS
-- Track per-agent performance over time
-- ============================================================
CREATE TABLE agent_metrics (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_name      TEXT NOT NULL,
    metric_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    actions_total   INTEGER DEFAULT 0,
    actions_auto    INTEGER DEFAULT 0,
    actions_approved INTEGER DEFAULT 0,
    actions_rejected INTEGER DEFAULT 0,
    actions_blocked  INTEGER DEFAULT 0,
    avg_confidence  FLOAT,
    avg_duration_ms INTEGER,
    errors_count    INTEGER DEFAULT 0,
    tokens_used     BIGINT DEFAULT 0,
    cost_usd        NUMERIC(10,4) DEFAULT 0,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_name, metric_date)
);

CREATE INDEX idx_metrics_agent ON agent_metrics (agent_name, metric_date DESC);

-- ============================================================
-- TABLE: KNOWLEDGE_BASE
-- Internal docs, policies, handbooks indexed for the Knowledge Agent
-- ============================================================
CREATE TABLE knowledge_base (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title       TEXT NOT NULL,
    category    TEXT NOT NULL,         -- 'policy', 'sop', 'handbook', 'agreement', 'faq'
    content     TEXT NOT NULL,
    source_url  TEXT,
    tags        JSONB DEFAULT '[]',
    version     TEXT DEFAULT '1.0',
    active      BOOLEAN DEFAULT TRUE,
    created_by  TEXT,
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_kb_category ON knowledge_base (category);
CREATE INDEX idx_kb_active   ON knowledge_base (active) WHERE active = TRUE;
CREATE INDEX idx_kb_tags     ON knowledge_base USING GIN (tags);
CREATE INDEX idx_kb_content  ON knowledge_base USING GIN (to_tsvector('english', content));

-- ============================================================
-- ANALYTICS VIEWS
-- ============================================================

-- People at risk of going cold (no contact in 14+ days, high priority)
CREATE MATERIALIZED VIEW mv_cold_relationships AS
SELECT
    p.id,
    p.name,
    p.email,
    p.type,
    p.status,
    p.priority,
    p.last_contact_at,
    EXTRACT(EPOCH FROM (NOW() - p.last_contact_at)) / 86400 AS days_since_contact
FROM people p
WHERE
    p.is_deleted = FALSE
    AND p.status IN ('active', 'prospect', 'contacted')
    AND p.priority >= 7
    AND (p.last_contact_at IS NULL OR p.last_contact_at < NOW() - INTERVAL '14 days')
ORDER BY p.priority DESC, days_since_contact DESC;

CREATE UNIQUE INDEX ON mv_cold_relationships (id);

-- Weekly pipeline health
CREATE VIEW v_pipeline_health AS
SELECT
    r.title AS role_title,
    r.status AS role_status,
    COUNT(cp.id) AS total_candidates,
    COUNT(CASE WHEN cp.stage = 'sourced' THEN 1 END) AS sourced,
    COUNT(CASE WHEN cp.stage = 'outreach' THEN 1 END) AS outreach_sent,
    COUNT(CASE WHEN cp.stage = 'responded' THEN 1 END) AS responded,
    COUNT(CASE WHEN cp.stage = 'screened' THEN 1 END) AS screened,
    COUNT(CASE WHEN cp.stage = 'interview' THEN 1 END) AS interviewing,
    COUNT(CASE WHEN cp.stage = 'offer' THEN 1 END) AS offers_made,
    COUNT(CASE WHEN cp.stage = 'hired' THEN 1 END) AS hired,
    COUNT(CASE WHEN cp.agent_recommendation = 'reject' THEN 1 END) AS rejected,
    ROUND(AVG(cp.fit_score)::NUMERIC, 2) AS avg_fit_score
FROM roles r
LEFT JOIN candidate_pipeline cp ON cp.role_id = r.id
GROUP BY r.id, r.title, r.status
ORDER BY r.opened_at DESC;

-- Agent activity summary (last 30 days)
CREATE VIEW v_agent_activity_30d AS
SELECT
    agent_name,
    action,
    COUNT(*) AS count,
    AVG(confidence) AS avg_confidence,
    AVG(duration_ms) AS avg_duration_ms,
    SUM(CASE WHEN success = FALSE THEN 1 ELSE 0 END) AS error_count
FROM audit_log
WHERE created_at >= NOW() - INTERVAL '30 days'
GROUP BY agent_name, action
ORDER BY agent_name, count DESC;

-- Pending approvals dashboard
CREATE VIEW v_pending_approvals AS
SELECT
    aa.*,
    p.name AS person_name,
    p.email AS person_email,
    p.type AS person_type
FROM agent_approvals aa
LEFT JOIN people p ON aa.person_id = p.id
WHERE aa.status = 'pending'
  AND aa.expires_at > NOW()
ORDER BY aa.created_at ASC;

-- Tasks due today or overdue
CREATE VIEW v_urgent_tasks AS
SELECT
    t.*,
    p.name AS person_name,
    p.email AS person_email
FROM tasks t
LEFT JOIN people p ON t.person_id = p.id
WHERE
    t.status NOT IN ('done', 'cancelled')
    AND t.due_at <= NOW() + INTERVAL '1 day'
ORDER BY t.due_at ASC NULLS LAST, t.priority DESC;

-- ============================================================
-- TRIGGERS
-- ============================================================

-- Auto-update updated_at on any row change
CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE TRIGGER set_updated_at_people
    BEFORE UPDATE ON people
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TRIGGER set_updated_at_tasks
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TRIGGER set_updated_at_roles
    BEFORE UPDATE ON roles
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TRIGGER set_updated_at_onboarding
    BEFORE UPDATE ON onboarding_plans
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TRIGGER set_updated_at_goals
    BEFORE UPDATE ON performance_goals
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TRIGGER set_updated_at_pipeline
    BEFORE UPDATE ON candidate_pipeline
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- Auto-log status changes on people table
CREATE OR REPLACE FUNCTION trigger_log_status_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO events (person_id, event_type, description, triggered_by, metadata)
        VALUES (
            NEW.id,
            'status_changed',
            format('Status changed from %s to %s', OLD.status, NEW.status),
            'system:trigger',
            jsonb_build_object('old_status', OLD.status, 'new_status', NEW.status)
        );
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER log_person_status_change
    AFTER UPDATE ON people
    FOR EACH ROW EXECUTE FUNCTION trigger_log_status_change();

-- Auto-refresh cold relationships view (call via pg_cron or n8n cron)
-- SELECT cron.schedule('refresh-cold-mv', '0 * * * *', 'REFRESH MATERIALIZED VIEW CONCURRENTLY mv_cold_relationships');

-- ============================================================
-- SEED: DEFAULT KNOWLEDGE BASE ENTRIES
-- ============================================================
INSERT INTO knowledge_base (title, category, content, tags) VALUES
(
    'Autonomy Policy — Three Tiers',
    'policy',
    'Auto-Execute (no approval needed): Draft communications, create/update records, generate docs, internal tasks.
Escalate (requires approval): Candidate shortlists, performance flags, proposed offers, contract changes.
Blocked (propose only): Final hiring/firing decisions, performance ratings tied to pay, legal or compliance actions.',
    '["autonomy", "policy", "agents", "guardrails"]'
),
(
    'Onboarding Checklist Template',
    'sop',
    'Standard onboarding checklist:
1. Welcome email sent with links to docs, channels, and first project brief.
2. GitHub/GitLab access granted to relevant repos.
3. Slack/Discord invite sent and accepted.
4. Notion/Confluence access provisioned.
5. Intro call with hiring manager scheduled (within first 3 days).
6. First sprint or project brief document shared.
7. Payment/invoicing setup confirmed.
8. 30-day check-in scheduled.',
    '["onboarding", "checklist", "sop"]'
),
(
    'Privacy and PII Handling Policy',
    'policy',
    'All agent systems must: (1) Never include raw PII in log outputs. (2) Mask or hash email and phone in audit trails. (3) Enforce 7-year audit retention. (4) Obtain consent before sending unsolicited outreach. (5) Delete or anonymize data upon request within 30 days.',
    '["privacy", "gdpr", "pii", "compliance"]'
);

-- ============================================================
-- HELPFUL QUERIES
-- ============================================================

-- COMMENT ON TABLE people IS 'Central people graph. Every contact, candidate, collaborator, client lives here.';
-- COMMENT ON TABLE interactions IS 'Every logged touchpoint with a person across all channels.';
-- COMMENT ON TABLE tasks IS 'Action items — created by agents or humans, with approval tier tracking.';
-- COMMENT ON TABLE audit_log IS 'Immutable append-only log of all agent and human actions. Partitioned by year for 7-year retention.';
-- COMMENT ON TABLE agent_approvals IS 'Human-in-the-loop queue: agent proposes, human approves/rejects.';
-- COMMENT ON TABLE candidate_pipeline IS 'Funnel tracking per candidate per role.';
-- COMMENT ON TABLE performance_goals IS 'Goals and deliverables per active collaborator, monitored weekly by Performance Agent.';
-- COMMENT ON TABLE knowledge_base IS 'Internal policy and SOP documents; full-text indexed for Knowledge Agent RAG.';
