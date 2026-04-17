"""
ReflectionEngine: generates verbal reflections on failures and injects them
into future agent calls as few-shot context.

Implements a simplified Reflexion framework (Shinn et al., NeurIPS 2023):
  Actor -> Evaluator -> Self-Reflection -> Actor (next call)

DB operations use SQLAlchemy async sessions matching the async codebase pattern.
Guarded by SELF_EVAL_ENABLED env var.
"""
import os

import structlog
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import text

log = structlog.get_logger()

REFLECTION_PROMPT = """You are a metacognitive coach for an AI Personnel Management agent.
An agent just produced a low-quality response. Your job: write a concise reflection that the agent
can read before its NEXT attempt at a similar task, so it avoids the same mistakes.

Agent: {agent}
Failure type: {failure_type}
Score: {score}/10
Critique: {critique}

Original input (summary): {input_summary}
Agent's bad output (summary): {output_summary}

Write a reflection in this format:
WHAT WENT WRONG: <1 sentence>
ROOT CAUSE: <1 sentence>
NEXT TIME: <2-3 bullet points of concrete instructions>

Be specific. Reference the actual failure, not generic advice."""

INJECTION_PREFIX = """[REFLECTION MEMORY — {n} recent lesson(s) for {agent} agent]
{reflections}
[END REFLECTIONS — apply these lessons to the current task]

"""


async def generate_reflection(
    agent: str,
    score: float,
    critique: str,
    failure_type: str | None,
    input_summary: str,
    output_summary: str,
) -> str:
    llm = ChatOpenAI(model="gpt-4.1-mini", temperature=0.3)
    prompt = ChatPromptTemplate.from_messages([("human", REFLECTION_PROMPT)])
    chain = prompt | llm
    result = await chain.ainvoke({
        "agent": agent,
        "failure_type": failure_type or "general",
        "score": score,
        "critique": critique,
        "input_summary": input_summary[:500],
        "output_summary": output_summary[:500],
    })
    return result.content


async def get_active_reflections(agent: str, session, limit: int = 3) -> str:
    """Retrieve top-N active reflections for an agent, formatted for prompt injection.

    Uses SQLAlchemy AsyncSession.
    """
    if os.getenv("SELF_EVAL_ENABLED", "false").lower() != "true":
        return ""

    if session is None:
        return ""

    try:
        result = await session.execute(
            text("""SELECT reflection FROM agent_reflections
                    WHERE agent = :agent AND is_active = TRUE
                    ORDER BY applied_count ASC, created_at DESC
                    LIMIT :limit"""),
            {"agent": agent, "limit": limit},
        )
        rows = result.fetchall()
    except Exception as exc:
        log.warning("reflection_fetch_failed", agent=agent, error=str(exc))
        return ""

    if not rows:
        return ""

    reflection_text = "\n\n".join(
        f"Lesson {i+1}:\n{r[0]}" for i, r in enumerate(rows)
    )
    return INJECTION_PREFIX.format(n=len(rows), agent=agent, reflections=reflection_text)


async def increment_applied_count(agent: str, session) -> None:
    """Bump applied_count for all active reflections of this agent.

    Uses SQLAlchemy AsyncSession.
    """
    if session is None:
        return

    try:
        await session.execute(
            text("UPDATE agent_reflections SET applied_count = applied_count + 1 "
                 "WHERE agent = :agent AND is_active = TRUE"),
            {"agent": agent},
        )
    except Exception as exc:
        log.warning("reflection_increment_failed", agent=agent, error=str(exc))
