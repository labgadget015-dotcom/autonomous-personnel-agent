"""
test_schema.py — Postgres schema migration validation
=====================================================
Verifies that db/schema.sql applies cleanly and all expected
tables, views, indexes, triggers, and partitions exist.
"""

import pytest
from conftest import db_conn  # noqa: F401  (re-exported for clarity)


EXPECTED_TABLES = {
    "people",
    "interactions",
    "tasks",
    "events",
    "roles",
    "candidate_pipeline",
    "onboarding_plans",
    "performance_goals",
    "agent_approvals",
    "audit_log",       # partitioned parent
    "agent_metrics",
    "knowledge_base",
}

EXPECTED_VIEWS = {
    "v_pipeline_health",
    "v_agent_activity_30d",
    "v_pending_approvals",
    "v_urgent_tasks",
}

EXPECTED_MATVIEWS = {
    "mv_cold_relationships",
}

EXPECTED_TRIGGERS = {
    "set_updated_at_people",
    "set_updated_at_tasks",
    "set_updated_at_roles",
    "set_updated_at_onboarding",
    "set_updated_at_goals",
    "set_updated_at_pipeline",
    "log_person_status_change",
}

EXPECTED_EXTENSIONS = {
    "uuid-ossp",
    "pg_trgm",
    "btree_gin",
}

EXPECTED_AUDIT_PARTITIONS = {
    "audit_log_2025",
    "audit_log_2026",
    "audit_log_2027",
    "audit_log_2028",
}


# ─────────────────────────────────────────────────
# Tables
# ─────────────────────────────────────────────────

class TestTables:
    def test_all_expected_tables_exist(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """)
            existing = {row[0] for row in cur.fetchall()}

        missing = EXPECTED_TABLES - existing
        assert not missing, f"Missing tables: {sorted(missing)}"

    def test_people_table_columns(self, db_conn):
        """Spot-check critical columns on the people table."""
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'people'
            """)
            cols = {row[0] for row in cur.fetchall()}

        required = {"id", "name", "email", "type", "status", "tags",
                    "priority", "last_contact_at", "created_at", "updated_at"}
        missing = required - cols
        assert not missing, f"people table missing columns: {missing}"

    def test_audit_log_is_partitioned(self, db_conn):
        """Confirm audit_log is a partitioned table."""
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT relkind FROM pg_class
                WHERE relname = 'audit_log' AND relnamespace = 'public'::regnamespace
            """)
            row = cur.fetchone()
        assert row is not None, "audit_log table not found"
        assert row[0] == 'p', "audit_log should be a partitioned table (relkind='p')"

    def test_audit_log_partitions_exist(self, db_conn):
        """Confirm expected yearly partitions exist."""
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT relname FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname = 'audit_log'
            """)
            partitions = {row[0] for row in cur.fetchall()}

        missing = EXPECTED_AUDIT_PARTITIONS - partitions
        assert not missing, f"Missing audit_log partitions: {sorted(missing)}"

    def test_knowledge_base_has_seed_data(self, db_conn):
        """Confirm the three seed policy entries were inserted."""
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM knowledge_base")
            count = cur.fetchone()[0]
        assert count >= 3, f"Expected at least 3 knowledge_base seed rows, got {count}"


# ─────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────

class TestViews:
    def test_all_views_exist(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.views
                WHERE table_schema = 'public'
            """)
            existing = {row[0] for row in cur.fetchall()}
        missing = EXPECTED_VIEWS - existing
        assert not missing, f"Missing views: {sorted(missing)}"

    def test_materialized_views_exist(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT matviewname FROM pg_matviews WHERE schemaname = 'public'
            """)
            existing = {row[0] for row in cur.fetchall()}
        missing = EXPECTED_MATVIEWS - existing
        assert not missing, f"Missing materialized views: {sorted(missing)}"

    def test_views_are_queryable(self, db_conn):
        """Confirm views don't raise errors when queried (even with no data)."""
        views_to_test = list(EXPECTED_VIEWS) + list(EXPECTED_MATVIEWS)
        for view in views_to_test:
            with db_conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {view} LIMIT 1")  # noqa: S608


# ─────────────────────────────────────────────────
# Triggers
# ─────────────────────────────────────────────────

class TestTriggers:
    def test_all_triggers_exist(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute("""
                SELECT trigger_name FROM information_schema.triggers
                WHERE trigger_schema = 'public'
            """)
            existing = {row[0] for row in cur.fetchall()}
        missing = EXPECTED_TRIGGERS - existing
        assert not missing, f"Missing triggers: {sorted(missing)}"

    def test_updated_at_trigger_fires(self, db_conn):
        """Insert a person, update it, confirm updated_at advances."""
        import time
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO people (name, type, status)
                VALUES ('Trigger Test Person', 'lead', 'prospect')
                RETURNING id, updated_at
            """)
            row = cur.fetchone()
            person_id, original_ts = row

            time.sleep(0.05)  # ensure clock advances

            cur.execute("UPDATE people SET notes = 'trigger test' WHERE id = %s", (person_id,))
            cur.execute("SELECT updated_at FROM people WHERE id = %s", (person_id,))
            new_ts = cur.fetchone()[0]

            # Cleanup
            cur.execute("DELETE FROM people WHERE id = %s", (person_id,))

        assert new_ts > original_ts, "updated_at trigger did not advance timestamp"

    def test_status_change_logs_event(self, db_conn):
        """Change person status and confirm an event row is logged."""
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO people (name, type, status)
                VALUES ('Status Event Test', 'lead', 'prospect')
                RETURNING id
            """)
            person_id = cur.fetchone()[0]

            cur.execute("UPDATE people SET status = 'contacted' WHERE id = %s", (person_id,))
            cur.execute("""
                SELECT event_type, description FROM events
                WHERE person_id = %s AND event_type = 'status_changed'
            """, (person_id,))
            event = cur.fetchone()

            # Cleanup
            cur.execute("DELETE FROM events  WHERE person_id = %s", (person_id,))
            cur.execute("DELETE FROM people WHERE id = %s", (person_id,))

        assert event is not None, "status_changed event was not logged by trigger"
        assert "prospect" in event[1] and "contacted" in event[1]


# ─────────────────────────────────────────────────
# Extensions
# ─────────────────────────────────────────────────

class TestExtensions:
    def test_required_extensions_installed(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute("SELECT extname FROM pg_extension")
            installed = {row[0] for row in cur.fetchall()}
        missing = EXPECTED_EXTENSIONS - installed
        assert not missing, f"Missing Postgres extensions: {missing}"


# ─────────────────────────────────────────────────
# Basic CRUD round-trip
# ─────────────────────────────────────────────────

class TestCRUD:
    def test_people_insert_and_query(self, db_conn):
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO people (name, email, type, status, tags, priority)
                VALUES ('Integration Test Person', 'it@test.local', 'lead', 'prospect',
                        '["python", "LLMs"]'::jsonb, 8)
                RETURNING id, name, tags
            """)
            row = cur.fetchone()
            person_id, name, tags = row

            assert name == "Integration Test Person"
            assert "python" in tags

            cur.execute("DELETE FROM people WHERE id = %s", (person_id,))

    def test_uuid_primary_keys(self, db_conn):
        """Confirm default UUID generation works on people table."""
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO people (name, type, status)
                VALUES ('UUID Test', 'lead', 'prospect')
                RETURNING id
            """)
            person_id = cur.fetchone()[0]
            assert len(str(person_id)) == 36, f"Expected UUID, got: {person_id}"
            cur.execute("DELETE FROM people WHERE id = %s", (person_id,))
