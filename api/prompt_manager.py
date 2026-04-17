"""
PromptManager: loads the active prompt version for each agent,
routes a percentage of traffic to candidate variants for A/B testing,
and exposes the current version string for logging.

All DB operations use psycopg2 (sync) matching the existing codebase.
"""
import os
import random

import structlog

log = structlog.get_logger()

# Default (seed) prompts — these are the starting versions.
# Once a row exists in prompt_versions for an agent, DB takes precedence.
DEFAULT_PROMPTS: dict[str, str] = {
    "talent": "You are a senior HR talent specialist. Evaluate candidates objectively against role requirements. Provide structured assessments with clear reasoning.",
    "scheduling": "You are an expert workforce scheduler. Summarise meetings concisely, identify action items, and draft professional calendar invitations.",
    "onboarding": "You are an experienced onboarding coordinator. Generate comprehensive onboarding plans with checklists, timelines, and access provisioning steps.",
    "performance": "You are a performance management expert. Analyse goals, identify risks early, and produce actionable weekly briefs for managers.",
    "knowledge": "You are a knowledge management specialist. Answer policy questions accurately using only provided documentation. Cite sources.",
    "route": "You are an intelligent request router for an HR system. Classify incoming events and route them to the correct specialist agent with appropriate context.",
    "guardrails": "You are a strict HR policy compliance officer. Flag PII exposure, bias, discriminatory language, and policy violations with zero tolerance.",
}

# Percentage of traffic routed to candidate variant during A/B testing (0-100)
AB_TEST_TRAFFIC_SPLIT = int(os.getenv("AB_TEST_SPLIT", "10"))


class PromptManager:
    def __init__(self, db_conn):
        """db_conn: psycopg2 connection object."""
        self._db = db_conn

    def get_prompt(self, agent: str) -> tuple[str, str]:
        """Returns (prompt_content, version_string). Handles A/B routing."""
        active = self._load_active(agent)
        candidate = self._load_candidate(agent)

        if candidate and random.randint(1, 100) <= AB_TEST_TRAFFIC_SPLIT:
            log.info("ab_test_routing", agent=agent, variant="candidate", version=candidate["version"])
            return candidate["content"], candidate["version"]

        return active["content"], active["version"]

    def get_active_prompt_raw(self, agent: str) -> tuple[str, str]:
        """Returns (prompt_content, version_string) for the active version only (no A/B)."""
        active = self._load_active(agent)
        return active["content"], active["version"]

    def _load_active(self, agent: str) -> dict:
        try:
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT version, content FROM prompt_versions WHERE agent = %s AND status = 'active'",
                    (agent,),
                )
                row = cur.fetchone()
        except Exception as exc:
            log.warning("prompt_load_active_failed", agent=agent, error=str(exc))
            row = None

        if row:
            return {"version": row[0], "content": row[1]}

        # Seed the DB with the default if no row exists
        self._seed_default(agent)
        return {"version": "1.0.0", "content": DEFAULT_PROMPTS.get(agent, "")}

    def _load_candidate(self, agent: str) -> dict | None:
        try:
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT version, content FROM prompt_versions WHERE agent = %s AND status = 'candidate'",
                    (agent,),
                )
                row = cur.fetchone()
        except Exception as exc:
            log.warning("prompt_load_candidate_failed", agent=agent, error=str(exc))
            return None

        return {"version": row[0], "content": row[1]} if row else None

    def _seed_default(self, agent: str) -> None:
        try:
            with self._db.cursor() as cur:
                cur.execute(
                    """INSERT INTO prompt_versions (agent, version, content, status, created_by)
                       VALUES (%s, '1.0.0', %s, 'active', 'system')
                       ON CONFLICT (agent, version) DO NOTHING""",
                    (agent, DEFAULT_PROMPTS.get(agent, "")),
                )
            self._db.commit()
        except Exception as exc:
            log.warning("prompt_seed_failed", agent=agent, error=str(exc))

    def record_outcome(self, agent: str, version: str, score: float) -> None:
        """Update rolling average score for a prompt version."""
        try:
            with self._db.cursor() as cur:
                cur.execute(
                    """UPDATE prompt_versions
                       SET score_avg = (COALESCE(score_avg, 0) * sample_count + %s) / (sample_count + 1),
                           sample_count = sample_count + 1
                       WHERE agent = %s AND version = %s""",
                    (score, agent, version),
                )
            self._db.commit()
        except Exception as exc:
            log.warning("prompt_record_outcome_failed", agent=agent, error=str(exc))

    def promote_candidate(self, agent: str) -> bool:
        """Promote candidate to active if its score_avg > active's score_avg."""
        try:
            with self._db.cursor() as cur:
                cur.execute(
                    "SELECT version, score_avg FROM prompt_versions WHERE agent = %s AND status = 'active'",
                    (agent,),
                )
                active = cur.fetchone()

                cur.execute(
                    "SELECT version, score_avg FROM prompt_versions WHERE agent = %s AND status = 'candidate'",
                    (agent,),
                )
                candidate = cur.fetchone()

            if not candidate or not active:
                return False

            active_score = active[1] or 0
            candidate_score = candidate[1] or 0

            if candidate_score > active_score:
                with self._db.cursor() as cur:
                    cur.execute(
                        "UPDATE prompt_versions SET status = 'archived', archived_at = NOW() "
                        "WHERE agent = %s AND status = 'active'",
                        (agent,),
                    )
                    cur.execute(
                        "UPDATE prompt_versions SET status = 'active', promoted_at = NOW() "
                        "WHERE agent = %s AND status = 'candidate'",
                        (agent,),
                    )
                self._db.commit()
                log.info(
                    "prompt_promoted",
                    agent=agent,
                    from_version=active[0],
                    to_version=candidate[0],
                    delta=round(candidate_score - active_score, 2),
                )
                return True
        except Exception as exc:
            log.warning("prompt_promote_failed", agent=agent, error=str(exc))

        return False
