"""
PromptManager: loads the active prompt version for each agent,
routes a percentage of traffic to candidate variants for A/B testing,
and exposes the current version string for logging.

All DB operations use SQLAlchemy async sessions.
"""
import os
import random

import structlog
from sqlalchemy import text

log = structlog.get_logger()

# Percentage of traffic routed to candidate variant during A/B testing (0-100)
AB_TEST_TRAFFIC_SPLIT = int(os.getenv("AB_TEST_SPLIT", "10"))


class PromptManager:
    # Default (seed) prompts -- these are the starting versions.
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

    def __init__(self):
        self._ab_split = AB_TEST_TRAFFIC_SPLIT

    async def get_prompt(self, agent: str, session) -> tuple[str, str]:
        """Returns (prompt_content, version_string). Handles A/B routing."""
        active = await self._load_active(agent, session)
        candidate = await self._load_candidate(agent, session)

        if candidate and random.randint(1, 100) <= self._ab_split:
            log.info("ab_test_routing", agent=agent, variant="candidate", version=candidate["version"])
            return candidate["content"], candidate["version"]

        return active["content"], active["version"]

    async def get_active_prompt_raw(self, agent: str, session) -> tuple[str, str]:
        """Returns (prompt_content, version_string) for the active version only (no A/B)."""
        active = await self._load_active(agent, session)
        return active["content"], active["version"]

    async def _load_active(self, agent: str, session) -> dict:
        try:
            result = await session.execute(
                text("SELECT version, content FROM prompt_versions WHERE agent = :agent AND status = 'active'"),
                {"agent": agent},
            )
            row = result.mappings().first()
        except Exception as exc:
            log.warning("prompt_load_active_failed", agent=agent, error=str(exc))
            row = None

        if row:
            return dict(row)

        # Seed the DB with the default if no row exists
        await self._seed_default(agent, session)
        return {"version": "1.0.0", "content": self.DEFAULT_PROMPTS.get(agent, "")}

    async def _load_candidate(self, agent: str, session) -> dict | None:
        try:
            result = await session.execute(
                text("SELECT version, content FROM prompt_versions WHERE agent = :agent AND status = 'candidate'"),
                {"agent": agent},
            )
            row = result.mappings().first()
        except Exception as exc:
            log.warning("prompt_load_candidate_failed", agent=agent, error=str(exc))
            return None

        return dict(row) if row else None

    async def _seed_default(self, agent: str, session) -> None:
        try:
            await session.execute(
                text("""INSERT INTO prompt_versions (agent, version, content, status, created_by)
                        VALUES (:agent, '1.0.0', :content, 'active', 'system')
                        ON CONFLICT (agent, version) DO NOTHING"""),
                {"agent": agent, "content": self.DEFAULT_PROMPTS.get(agent, "")},
            )
        except Exception as exc:
            log.warning("prompt_seed_failed", agent=agent, error=str(exc))

    async def record_outcome(self, agent: str, version: str, score: float, session) -> None:
        """Update rolling average score for a prompt version."""
        try:
            await session.execute(
                text("""UPDATE prompt_versions
                        SET score_avg = (COALESCE(score_avg, 0) * sample_count + :score) / (sample_count + 1),
                            sample_count = sample_count + 1
                        WHERE agent = :agent AND version = :version"""),
                {"score": score, "agent": agent, "version": version},
            )
        except Exception as exc:
            log.warning("prompt_record_outcome_failed", agent=agent, error=str(exc))

    async def promote_candidate(self, agent: str, session) -> bool:
        """Promote candidate to active if its score_avg > active's score_avg."""
        try:
            result_active = await session.execute(
                text("SELECT version, score_avg FROM prompt_versions WHERE agent = :agent AND status = 'active'"),
                {"agent": agent},
            )
            active = result_active.mappings().first()

            result_candidate = await session.execute(
                text("SELECT version, score_avg FROM prompt_versions WHERE agent = :agent AND status = 'candidate'"),
                {"agent": agent},
            )
            candidate = result_candidate.mappings().first()

            if not candidate or not active:
                return False

            active_score = active["score_avg"] or 0
            candidate_score = candidate["score_avg"] or 0

            if candidate_score > active_score:
                await session.execute(
                    text("UPDATE prompt_versions SET status = 'archived', archived_at = NOW() "
                         "WHERE agent = :agent AND status = 'active'"),
                    {"agent": agent},
                )
                await session.execute(
                    text("UPDATE prompt_versions SET status = 'active', promoted_at = NOW() "
                         "WHERE agent = :agent AND status = 'candidate'"),
                    {"agent": agent},
                )
                log.info(
                    "prompt_promoted",
                    agent=agent,
                    from_version=active["version"],
                    to_version=candidate["version"],
                    delta=round(float(candidate_score) - float(active_score), 2),
                )
                return True
        except Exception as exc:
            log.warning("prompt_promote_failed", agent=agent, error=str(exc))

        return False
