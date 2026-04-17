"""self-refining agent system tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-17
"""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS agent_outcomes (
        id              BIGSERIAL PRIMARY KEY,
        trace_id        TEXT,
        request_id      TEXT,
        agent           TEXT NOT NULL,
        job_id          TEXT,
        input_hash      TEXT,
        score           NUMERIC(4,2) NOT NULL,
        passed          BOOLEAN NOT NULL,
        critique        TEXT,
        rubric_scores   JSONB,
        prompt_version  TEXT,
        model           TEXT,
        tokens_total    INTEGER,
        latency_ms      INTEGER,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_agent_ts ON agent_outcomes(agent, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_passed ON agent_outcomes(agent, passed, created_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS agent_reflections (
        id              BIGSERIAL PRIMARY KEY,
        agent           TEXT NOT NULL,
        context_hash    TEXT NOT NULL,
        reflection      TEXT NOT NULL,
        failure_type    TEXT,
        source_outcome  BIGINT REFERENCES agent_outcomes(id),
        applied_count   INTEGER DEFAULT 0,
        is_active       BOOLEAN DEFAULT TRUE,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_reflections_agent_active ON agent_reflections(agent, is_active)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS prompt_versions (
        id              BIGSERIAL PRIMARY KEY,
        agent           TEXT NOT NULL,
        version         TEXT NOT NULL,
        content         TEXT NOT NULL,
        status          TEXT NOT NULL CHECK (status IN ('active','candidate','archived','rollback')),
        score_avg       NUMERIC(4,2),
        sample_count    INTEGER DEFAULT 0,
        promoted_at     TIMESTAMPTZ,
        archived_at     TIMESTAMPTZ,
        created_by      TEXT DEFAULT 'system',
        notes           TEXT,
        created_at      TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(agent, version)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_prompt_versions_agent_status ON prompt_versions(agent, status)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS eval_runs (
        id              BIGSERIAL PRIMARY KEY,
        agent           TEXT NOT NULL,
        variant_a       TEXT NOT NULL,
        variant_b       TEXT NOT NULL,
        score_a         NUMERIC(4,2),
        score_b         NUMERIC(4,2),
        sample_count    INTEGER,
        winner          TEXT,
        delta           NUMERIC(4,2),
        promoted        BOOLEAN DEFAULT FALSE,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS eval_runs")
    op.execute("DROP TABLE IF EXISTS agent_reflections")
    op.execute("DROP TABLE IF EXISTS prompt_versions")
    op.execute("DROP TABLE IF EXISTS agent_outcomes")
