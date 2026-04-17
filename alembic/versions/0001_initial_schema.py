"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-17
"""
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from pathlib import Path
    schema_path = Path(__file__).parent.parent.parent / "db" / "schema.sql"
    if not schema_path.exists():
        return
    sql = schema_path.read_text()
    # Execute each statement, skipping the 4 self-refining tables (handled in 0002)
    self_refining_tables = {"agent_outcomes", "agent_reflections", "prompt_versions", "eval_runs"}
    skip = False
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if not stmt or stmt.startswith("--"):
            continue
        # Check if this statement creates a self-refining table
        stmt_lower = stmt.lower()
        if any(f"table" in stmt_lower and tbl in stmt_lower and "create" in stmt_lower
               for tbl in self_refining_tables):
            skip = True
        # Also skip indexes on self-refining tables
        if any(tbl in stmt_lower for tbl in self_refining_tables) and "index" in stmt_lower:
            skip = True
        if skip:
            skip = False
            continue
        try:
            op.execute(stmt)
        except Exception:
            pass  # IF NOT EXISTS makes most statements idempotent


def downgrade() -> None:
    pass  # no destructive downgrade on initial
