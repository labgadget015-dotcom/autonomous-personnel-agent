"""
PromptRefiner: arq cron job (every 6h) that:
1. Analyses low-scoring outcomes from the past 24h
2. Clusters failures by failure_type
3. Asks an LLM to propose an improved system prompt
4. Writes it as a 'candidate' version for A/B testing

DB operations use SQLAlchemy async sessions.
Guarded by PROMPT_REFINE_ENABLED env var.
"""
import os

import structlog
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import text

from prompt_manager import PromptManager

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


async def run_prompt_refiner(ctx):
    """arq cron job: called every 6h. Refines prompts for underperforming agents."""
    if os.getenv("PROMPT_REFINE_ENABLED", "false").lower() != "true":
        log.info("prompt_refiner_skipped", reason="PROMPT_REFINE_ENABLED != true")
        return

    min_failures = int(os.getenv("REFINE_MIN_FAILURES", "3"))
    refine_model = os.getenv("REFINE_MODEL", "gpt-4.1")

    from db import session_ctx

    async with session_ctx() as session:
        if session is None:
            log.warning("prompt_refiner_skipped", reason="database_unavailable")
            return

        prompt_mgr = PromptManager()

        result = await session.execute(
            text("""SELECT agent, COUNT(*) as failures, AVG(score) as avg_score,
                          array_agg(DISTINCT critique) as critiques
                   FROM agent_outcomes
                   WHERE created_at > NOW() - INTERVAL '24 hours'
                     AND passed = FALSE
                   GROUP BY agent
                   HAVING COUNT(*) >= :min_failures"""),
            {"min_failures": min_failures},
        )
        agents_to_refine = result.mappings().all()

        for row in agents_to_refine:
            agent = row["agent"]
            log.info(
                "refiner_start",
                agent=agent,
                failures=row["failures"],
                avg_score=round(float(row["avg_score"]), 2),
            )

            active_prompt, current_version = await prompt_mgr.get_active_prompt_raw(agent, session)

            # Get active reflections for context
            refl_result = await session.execute(
                text("SELECT reflection FROM agent_reflections WHERE agent = :agent AND is_active = TRUE LIMIT 5"),
                {"agent": agent},
            )
            reflections = refl_result.fetchall()
            reflection_text = "\n".join(f"- {r[0][:200]}" for r in reflections)

            # Get failure type breakdown
            type_result = await session.execute(
                text("""SELECT failure_type, COUNT(*) as cnt
                        FROM agent_outcomes
                        WHERE agent = :agent AND created_at > NOW() - INTERVAL '24 hours' AND passed = FALSE
                        GROUP BY failure_type"""),
                {"agent": agent},
            )
            type_rows = type_result.mappings().all()
            failure_breakdown = ", ".join(
                f"{r['failure_type'] or 'general'}: {r['cnt']}" for r in type_rows
            )

            top_critiques = "\n".join(
                f"- {c[:150]}" for c in (row["critiques"] or [])[:5] if c
            )

            llm = ChatOpenAI(model=refine_model, temperature=0.4)
            prompt = ChatPromptTemplate.from_messages([("human", REFINE_PROMPT)])
            chain = prompt | llm

            llm_result = await chain.ainvoke({
                "agent": agent,
                "current_version": current_version,
                "current_prompt": active_prompt[:2000],
                "failure_count": row["failures"],
                "avg_score": round(float(row["avg_score"]), 2),
                "failure_breakdown": failure_breakdown,
                "top_critiques": top_critiques,
                "reflections": reflection_text or "None yet.",
            })

            new_prompt = llm_result.content.strip()
            # Increment minor version: '1.0.0' -> '1.1.0'
            parts = current_version.split(".")
            new_version = f"{parts[0]}.{int(parts[1]) + 1}.0"

            await session.execute(
                text("""INSERT INTO prompt_versions (agent, version, content, status, created_by, notes)
                        VALUES (:agent, :version, :content, 'candidate', 'system', :notes)
                        ON CONFLICT (agent, version) DO UPDATE
                            SET content = EXCLUDED.content, status = 'candidate'"""),
                {
                    "agent": agent,
                    "version": new_version,
                    "content": new_prompt,
                    "notes": f"Auto-generated from {row['failures']} failures (avg {round(float(row['avg_score']), 2)}/10)",
                },
            )

            log.info("refiner_candidate_created", agent=agent, version=new_version)

        # Promote any candidates that have accumulated enough sample data and beat active
        cand_result = await session.execute(
            text("SELECT DISTINCT agent FROM prompt_versions WHERE status = 'candidate'")
        )
        candidate_agents = cand_result.fetchall()

        for agent_row in candidate_agents:
            promoted = await prompt_mgr.promote_candidate(agent_row[0], session)
            if promoted:
                log.info("refiner_promotion_complete", agent=agent_row[0])
