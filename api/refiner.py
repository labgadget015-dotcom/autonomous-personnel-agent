"""
PromptRefiner: arq cron job (every 6h) that:
1. Analyses low-scoring outcomes from the past 24h
2. Clusters failures by failure_type
3. Asks an LLM to propose an improved system prompt
4. Writes it as a 'candidate' version for A/B testing

DB operations use psycopg2 (sync) matching the existing codebase.
Guarded by PROMPT_REFINE_ENABLED env var.
"""
import os

import psycopg2
import psycopg2.extras
import structlog
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from api.prompt_manager import PromptManager

log = structlog.get_logger()

REFINE_PROMPT = """You are an expert prompt engineer for an AI Personnel Management system.

Current system prompt for the {agent} agent (version {current_version}):
---
{current_prompt}
---

Recent failure analysis (last 24h, {failure_count} low-scoring calls):
- Average score: {avg_score}/10
- Failure breakdown: {failure_breakdown}
- Common critiques:
{top_critiques}

Active reflection memory (lessons learned):
{reflections}

Your task: write an IMPROVED version of the system prompt that:
1. Directly addresses the failure patterns above
2. Incorporates the reflection lessons
3. Preserves all correct existing behaviour
4. Is specific and actionable — not vague

Return ONLY the new system prompt text, nothing else."""


def _get_refiner_db():
    """Create a psycopg2 connection for the refiner job."""
    database_url = os.getenv("DATABASE_URL", "postgresql://agent:agent@localhost:5432/personnel_agent")
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    return conn


async def run_prompt_refiner(ctx):
    """arq cron job: called every 6h. Refines prompts for underperforming agents."""
    if os.getenv("PROMPT_REFINE_ENABLED", "false").lower() != "true":
        log.info("prompt_refiner_skipped", reason="PROMPT_REFINE_ENABLED != true")
        return

    min_failures = int(os.getenv("REFINE_MIN_FAILURES", "3"))
    refine_model = os.getenv("REFINE_MODEL", "gpt-4.1")

    conn = _get_refiner_db()
    try:
        prompt_mgr = PromptManager(conn)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """SELECT agent, COUNT(*) as failures, AVG(score) as avg_score,
                          array_agg(DISTINCT critique) as critiques
                   FROM agent_outcomes
                   WHERE created_at > NOW() - INTERVAL '24 hours'
                     AND passed = FALSE
                   GROUP BY agent
                   HAVING COUNT(*) >= %s""",
                (min_failures,),
            )
            agents_to_refine = cur.fetchall()

        for row in agents_to_refine:
            agent = row["agent"]
            log.info(
                "refiner_start",
                agent=agent,
                failures=row["failures"],
                avg_score=round(float(row["avg_score"]), 2),
            )

            active_prompt, current_version = prompt_mgr.get_active_prompt_raw(agent)

            # Get active reflections for context
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT reflection FROM agent_reflections WHERE agent = %s AND is_active = TRUE LIMIT 5",
                    (agent,),
                )
                reflections = cur.fetchall()
            reflection_text = "\n".join(f"- {r[0][:200]}" for r in reflections)

            # Get failure type breakdown
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    """SELECT failure_type, COUNT(*) as cnt
                       FROM agent_outcomes
                       WHERE agent = %s AND created_at > NOW() - INTERVAL '24 hours' AND passed = FALSE
                       GROUP BY failure_type""",
                    (agent,),
                )
                type_rows = cur.fetchall()
            failure_breakdown = ", ".join(
                f"{r['failure_type'] or 'general'}: {r['cnt']}" for r in type_rows
            )

            top_critiques = "\n".join(
                f"- {c[:150]}" for c in (row["critiques"] or [])[:5] if c
            )

            llm = ChatOpenAI(model=refine_model, temperature=0.4)
            prompt = ChatPromptTemplate.from_messages([("human", REFINE_PROMPT)])
            chain = prompt | llm

            result = await chain.ainvoke({
                "agent": agent,
                "current_version": current_version,
                "current_prompt": active_prompt[:2000],
                "failure_count": row["failures"],
                "avg_score": round(float(row["avg_score"]), 2),
                "failure_breakdown": failure_breakdown,
                "top_critiques": top_critiques,
                "reflections": reflection_text or "None yet.",
            })

            new_prompt = result.content.strip()
            # Increment minor version: '1.0.0' -> '1.1.0'
            parts = current_version.split(".")
            new_version = f"{parts[0]}.{int(parts[1]) + 1}.0"

            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO prompt_versions (agent, version, content, status, created_by, notes)
                       VALUES (%s, %s, %s, 'candidate', 'system', %s)
                       ON CONFLICT (agent, version) DO UPDATE
                           SET content = EXCLUDED.content, status = 'candidate'""",
                    (
                        agent,
                        new_version,
                        new_prompt,
                        f"Auto-generated from {row['failures']} failures (avg {round(float(row['avg_score']), 2)}/10)",
                    ),
                )
            conn.commit()

            log.info("refiner_candidate_created", agent=agent, version=new_version)

        # Promote any candidates that have accumulated enough sample data and beat active
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT agent FROM prompt_versions WHERE status = 'candidate'")
            candidate_agents = cur.fetchall()

        for agent_row in candidate_agents:
            promoted = prompt_mgr.promote_candidate(agent_row[0])
            if promoted:
                log.info("refiner_promotion_complete", agent=agent_row[0])

    finally:
        conn.close()
