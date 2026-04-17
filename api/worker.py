"""
worker.py — arq Task Queue Worker
===================================
Offloads long-running agent calls to a Redis-backed async worker pool,
preventing HTTP gateway timeouts (30s) on multi-hop LLM chains that can
take 5–15 seconds per call.

Architecture:
  1. API receives request -> enqueues job -> returns {job_id, status: "queued"}
  2. Worker picks up job -> runs agent -> stores result in Redis (TTL 1 hour)
  3. Caller polls GET /jobs/{job_id} -> receives status + result when complete

Usage:
  Start the worker alongside the API:
    python -m arq api.worker.WorkerSettings
  Or via docker-compose (see worker service definition).

Job functions exposed (one per long-running agent action):
  - run_talent_screen
  - run_talent_outreach
  - run_talent_pipeline_summary
  - run_scheduling_summarise
  - run_scheduling_followups
  - run_scheduling_invite
  - run_onboarding_plan
  - run_onboarding_check
  - run_onboarding_offboarding
  - run_performance_brief
  - run_performance_goal_risk
  - run_knowledge_answer
  - run_knowledge_generate
  - run_route
  - run_evaluate_outcome    (self-eval: scores agent output)
  - run_prompt_refiner      (cron: refines prompts every 6h)
"""

import json
import os
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from arq.connections import RedisSettings
from arq.cron import cron

logger = logging.getLogger("personnel-agent.worker")

# OTel tracer — no-op if package not installed
try:
    from telemetry import get_tracer, configure_tracing
    _tracer = get_tracer("personnel-agent.worker")
except ImportError:
    from telemetry import _NoOpTracer  # type: ignore
    _tracer = _NoOpTracer()

# Helper to get current trace ID (safe if OTel not available)
def _current_trace_id() -> str:
    try:
        from telemetry import current_trace_id
        return current_trace_id()
    except Exception:
        return ""


# ============================================================
# Redis configuration
# ============================================================

def _redis_settings() -> RedisSettings:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    # Parse redis://host:port or redis://:password@host:port
    try:
        from urllib.parse import urlparse
        parsed = urlparse(redis_url)
        return RedisSettings(
            host=parsed.hostname or "localhost",
            port=parsed.port or 6379,
            password=parsed.password or None,
            database=int(parsed.path.lstrip("/") or 0),
        )
    except Exception:
        return RedisSettings(host="localhost", port=6379)


# ============================================================
# Self-eval helpers
# ============================================================

def _self_eval_enabled() -> bool:
    return os.getenv("SELF_EVAL_ENABLED", "false").lower() == "true"


async def _inject_reflections(agent: str) -> str:
    """Load active reflections for the agent using async session. Returns prefix string."""
    if not _self_eval_enabled():
        return ""
    try:
        from db import session_ctx
        from reflection import get_active_reflections, increment_applied_count
        async with session_ctx() as session:
            if session is None:
                return ""
            prefix = await get_active_reflections(agent, session)
            if prefix:
                await increment_applied_count(agent, session)
            return prefix
    except Exception as exc:
        logger.warning("Reflection injection failed for %s: %s", agent, exc)
        return ""


async def _enqueue_eval(ctx, agent: str, user_input, agent_output, prompt_version: str, request_id: str):
    """Fire-and-forget: enqueue an evaluation job. Never blocks the main result."""
    if not _self_eval_enabled():
        return
    try:
        pool = ctx.get("arq_pool")
        if pool is None:
            # Try to use the arq pool from the worker context
            from arq import create_pool
            pool = await create_pool(_redis_settings())
        await pool.enqueue_job(
            "run_evaluate_outcome",
            agent=agent,
            user_input=user_input,
            agent_output=agent_output,
            prompt_version=prompt_version,
            trace_id=_current_trace_id(),
            request_id=request_id,
        )
    except Exception:
        pass  # never block the main result


# ============================================================
# Worker startup / shutdown hooks
# ============================================================

async def startup(ctx: Dict[str, Any]) -> None:
    """Initialise shared resources — agents, OTel tracing, async DB, logging."""
    from agents import (
        OrchestratorAgent,
        TalentAgent,
        SchedulingAgent,
        OnboardingAgent,
        PerformanceAgent,
        KnowledgeAgent,
    )
    # Configure OTel on the worker process (separate process from API)
    try:
        from telemetry import configure_tracing
        configure_tracing()   # no app= in worker — instruments Redis + SQLAlchemy only
    except Exception as exc:
        logger.warning("OTel tracing not configured in worker: %s", exc)

    # Async DB layer
    from db import init_db
    await init_db(os.environ.get("DATABASE_URL"))

    logger.info("arq worker starting up — initialising agents")
    ctx["orchestrator"] = OrchestratorAgent()
    ctx["talent"]       = TalentAgent()
    ctx["scheduling"]   = SchedulingAgent()
    ctx["onboarding"]   = OnboardingAgent()
    ctx["performance"]  = PerformanceAgent()
    ctx["knowledge"]    = KnowledgeAgent(
        vectorstore_path=os.getenv("KNOWLEDGE_VECTORSTORE_PATH")
    )

    logger.info("arq worker ready — all agents initialised")


async def shutdown(ctx: Dict[str, Any]) -> None:
    logger.info("arq worker shutting down")
    from db import close_db
    await close_db()


# ============================================================
# Job functions — Orchestrator
# ============================================================

async def run_route(ctx: Dict[str, Any], payload: Dict[str, Any],
                    _request_id: Optional[str] = None) -> Dict[str, Any]:
    """Route an event through the orchestrator."""
    import uuid
    job_request_id = _request_id or str(uuid.uuid4())
    with _tracer.start_as_current_span("worker.run_route") as span:
        span.set_attribute("agent", "orchestrator")
        span.set_attribute("request_id", job_request_id)
        import structlog
        structlog.contextvars.bind_contextvars(request_id=job_request_id, agent="orchestrator")

        # Self-refining: inject reflections
        prompt_version = "1.0.0"
        await _inject_reflections("route")

        result = ctx["orchestrator"].route(payload)
        result["request_id"] = job_request_id

        # Self-refining: fire-and-forget evaluation
        await _enqueue_eval(ctx, "route", payload, result, prompt_version, job_request_id)

    return result


# ============================================================
# Job functions — Talent Agent
# ============================================================

async def run_talent_screen(
    ctx: Dict[str, Any],
    candidate_profile: Dict[str, Any],
    role: Dict[str, Any],
    history: Optional[List[Dict]] = None,
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_talent_screen") as span:
        span.set_attribute("agent", "talent")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="talent")

        prompt_version = "1.0.0"
        await _inject_reflections("talent")

        result = ctx["talent"].screen_candidate(candidate_profile, role, history)

        await _enqueue_eval(ctx, "talent", {"candidate_profile": candidate_profile, "role": role}, result, prompt_version, _request_id or "")

    return result


async def run_talent_outreach(
    ctx: Dict[str, Any],
    candidate: Dict[str, Any],
    role: Dict[str, Any],
    tone: str = "professional_warm",
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_talent_outreach") as span:
        span.set_attribute("agent", "talent")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="talent")

        prompt_version = "1.0.0"
        await _inject_reflections("talent")

        result = ctx["talent"].draft_outreach(candidate, role, tone)

        await _enqueue_eval(ctx, "talent", {"candidate": candidate, "role": role, "tone": tone}, result, prompt_version, _request_id or "")

    return result


async def run_talent_pipeline_summary(
    ctx: Dict[str, Any],
    pipeline_data: List[Dict[str, Any]],
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_talent_pipeline_summary") as span:
        span.set_attribute("agent", "talent")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="talent")

        prompt_version = "1.0.0"
        await _inject_reflections("talent")

        result = ctx["talent"].build_pipeline_summary(pipeline_data)

        await _enqueue_eval(ctx, "talent", {"pipeline_data": pipeline_data}, result, prompt_version, _request_id or "")

    return result


# ============================================================
# Job functions — Scheduling Agent
# ============================================================

async def run_scheduling_summarise(
    ctx: Dict[str, Any],
    meeting_notes: str,
    attendees: List[Dict[str, Any]],
    context: Optional[Dict[str, Any]] = None,
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_scheduling_summarise") as span:
        span.set_attribute("agent", "scheduling")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="scheduling")

        prompt_version = "1.0.0"
        await _inject_reflections("scheduling")

        result = ctx["scheduling"].summarise_meeting(meeting_notes, attendees, context)

        await _enqueue_eval(ctx, "scheduling", {"meeting_notes": meeting_notes, "attendees": attendees}, result, prompt_version, _request_id or "")

    return result


async def run_scheduling_followups(
    ctx: Dict[str, Any],
    people: List[Dict[str, Any]],
    threshold_days: int = 14,
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_scheduling_followups") as span:
        span.set_attribute("agent", "scheduling")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="scheduling")

        prompt_version = "1.0.0"
        await _inject_reflections("scheduling")

        result = ctx["scheduling"].identify_cold_followups(people, threshold_days)

        await _enqueue_eval(ctx, "scheduling", {"people": people, "threshold_days": threshold_days}, result, prompt_version, _request_id or "")

    return result


async def run_scheduling_invite(
    ctx: Dict[str, Any],
    purpose: str,
    attendees: List[Dict[str, Any]],
    duration_minutes: int = 30,
    context: Optional[Dict[str, Any]] = None,
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_scheduling_invite") as span:
        span.set_attribute("agent", "scheduling")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="scheduling")

        prompt_version = "1.0.0"
        await _inject_reflections("scheduling")

        result = ctx["scheduling"].draft_meeting_invite(purpose, attendees, duration_minutes, context)

        await _enqueue_eval(ctx, "scheduling", {"purpose": purpose, "attendees": attendees}, result, prompt_version, _request_id or "")

    return result


# ============================================================
# Job functions — Onboarding Agent
# ============================================================

async def run_onboarding_plan(
    ctx: Dict[str, Any],
    person: Dict[str, Any],
    role: Dict[str, Any],
    tools: List[str],
    company_links: Optional[Dict[str, Any]] = None,
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_onboarding_plan") as span:
        span.set_attribute("agent", "onboarding")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="onboarding")

        prompt_version = "1.0.0"
        await _inject_reflections("onboarding")

        result = ctx["onboarding"].generate_onboarding_plan(person, role, tools, company_links)

        await _enqueue_eval(ctx, "onboarding", {"person": person, "role": role, "tools": tools}, result, prompt_version, _request_id or "")

    return result


async def run_onboarding_check(
    ctx: Dict[str, Any],
    plan: Dict[str, Any],
    days_since_start: int,
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_onboarding_check") as span:
        span.set_attribute("agent", "onboarding")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="onboarding")

        prompt_version = "1.0.0"
        await _inject_reflections("onboarding")

        result = ctx["onboarding"].check_onboarding_progress(plan, days_since_start)

        await _enqueue_eval(ctx, "onboarding", {"plan": plan, "days_since_start": days_since_start}, result, prompt_version, _request_id or "")

    return result


async def run_onboarding_offboarding(
    ctx: Dict[str, Any],
    person: Dict[str, Any],
    reason: str,
    last_day: str,
    access_to_revoke: List[str],
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_onboarding_offboarding") as span:
        span.set_attribute("agent", "onboarding")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="onboarding")

        prompt_version = "1.0.0"
        await _inject_reflections("onboarding")

        result = ctx["onboarding"].generate_offboarding_plan(person, reason, last_day, access_to_revoke)

        await _enqueue_eval(ctx, "onboarding", {"person": person, "reason": reason}, result, prompt_version, _request_id or "")

    return result


# ============================================================
# Job functions — Performance Agent
# ============================================================

async def run_performance_brief(
    ctx: Dict[str, Any],
    people: List[Dict[str, Any]],
    goals: List[Dict[str, Any]],
    recent_interactions: List[Dict[str, Any]],
    github_activity: Optional[Dict[str, Any]] = None,
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_performance_brief") as span:
        span.set_attribute("agent", "performance")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="performance")

        prompt_version = "1.0.0"
        await _inject_reflections("performance")

        result = ctx["performance"].generate_weekly_brief(people, goals, recent_interactions, github_activity)

        await _enqueue_eval(ctx, "performance", {"people": people, "goals": goals}, result, prompt_version, _request_id or "")

    return result


async def run_performance_goal_risk(
    ctx: Dict[str, Any],
    goal: Dict[str, Any],
    person: Dict[str, Any],
    interactions: List[Dict[str, Any]],
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_performance_goal_risk") as span:
        span.set_attribute("agent", "performance")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="performance")

        prompt_version = "1.0.0"
        await _inject_reflections("performance")

        result = ctx["performance"].assess_goal_risk(goal, person, interactions)

        await _enqueue_eval(ctx, "performance", {"goal": goal, "person": person}, result, prompt_version, _request_id or "")

    return result


# ============================================================
# Job functions — Knowledge Agent
# ============================================================

async def run_knowledge_answer(
    ctx: Dict[str, Any],
    question: str,
    raw_docs: Optional[List[str]] = None,
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_knowledge_answer") as span:
        span.set_attribute("agent", "knowledge")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="knowledge")

        prompt_version = "1.0.0"
        await _inject_reflections("knowledge")

        result = ctx["knowledge"].answer_policy_question(question, raw_docs)

        await _enqueue_eval(ctx, "knowledge", {"question": question}, result, prompt_version, _request_id or "")

    return result


async def run_knowledge_generate(
    ctx: Dict[str, Any],
    doc_type: str,
    context: Dict[str, Any],
    _request_id: Optional[str] = None,
) -> Dict[str, Any]:
    with _tracer.start_as_current_span("worker.run_knowledge_generate") as span:
        span.set_attribute("agent", "knowledge")
        if _request_id:
            span.set_attribute("request_id", _request_id)
            import structlog
            structlog.contextvars.bind_contextvars(request_id=_request_id, agent="knowledge")

        prompt_version = "1.0.0"
        await _inject_reflections("knowledge")

        result = ctx["knowledge"].generate_document(doc_type, context)

        await _enqueue_eval(ctx, "knowledge", {"doc_type": doc_type, "context": context}, result, prompt_version, _request_id or "")

    return result


# ============================================================
# Self-Eval Job: evaluate agent output and store result
# ============================================================

async def run_evaluate_outcome(
    ctx: Dict[str, Any],
    agent: str,
    user_input: dict,
    agent_output: dict,
    prompt_version: str = "1.0.0",
    trace_id: str = "",
    request_id: str = "",
) -> dict:
    """Evaluate a completed agent output and store result + reflection if needed."""
    import structlog
    from evaluation import evaluate_output
    from reflection import generate_reflection
    from db import session_ctx
    log = structlog.get_logger()

    eval_result = await evaluate_output(
        agent=agent,
        user_input=user_input,
        agent_output=agent_output,
        prompt_version=prompt_version,
    )

    async with session_ctx() as session:
        if session:
            try:
                await session.execute(
                    text("""INSERT INTO agent_outcomes
                           (agent, trace_id, request_id, input_hash, score, passed, critique,
                            rubric_scores, prompt_version, model)
                           VALUES (:agent, :trace_id, :request_id, :input_hash, :score,
                                   :passed, :critique, :rubric_scores, :prompt_version, :model)"""),
                    {
                        "agent": agent, "trace_id": trace_id, "request_id": request_id,
                        "input_hash": eval_result.input_hash, "score": eval_result.score,
                        "passed": eval_result.passed, "critique": eval_result.critique,
                        "rubric_scores": json.dumps(eval_result.rubric),
                        "prompt_version": prompt_version, "model": "gpt-4.1-mini",
                    },
                )
            except Exception as exc:
                log.warning("eval_outcome_write_failed", agent=agent, error=str(exc))

            # Generate reflection if failed
            if not eval_result.passed:
                try:
                    reflection = await generate_reflection(
                        agent=agent,
                        score=eval_result.score,
                        critique=eval_result.critique,
                        failure_type=eval_result.failure_type,
                        input_summary=str(user_input)[:300],
                        output_summary=str(agent_output)[:300],
                    )
                    await session.execute(
                        text("""INSERT INTO agent_reflections (agent, context_hash, reflection, failure_type)
                               VALUES (:agent, :ctx_hash, :reflection, :failure_type)"""),
                        {
                            "agent": agent, "ctx_hash": eval_result.input_hash,
                            "reflection": reflection, "failure_type": eval_result.failure_type,
                        },
                    )
                except Exception as exc:
                    log.warning("eval_reflection_write_failed", agent=agent, error=str(exc))

    log.info(
        "eval_complete",
        agent=agent,
        score=eval_result.score,
        passed=eval_result.passed,
        failure_type=eval_result.failure_type,
    )
    return {"score": eval_result.score, "passed": eval_result.passed}


# ============================================================
# Prompt Refiner (imported from api.refiner, registered as cron)
# ============================================================

from refiner import run_prompt_refiner


# ============================================================
# Worker settings
# ============================================================

class WorkerSettings:
    """
    arq worker configuration.
    Start with: python -m arq api.worker.WorkerSettings
    """

    # All async job functions registered on this worker
    functions = [
        run_route,
        run_talent_screen,
        run_talent_outreach,
        run_talent_pipeline_summary,
        run_scheduling_summarise,
        run_scheduling_followups,
        run_scheduling_invite,
        run_onboarding_plan,
        run_onboarding_check,
        run_onboarding_offboarding,
        run_performance_brief,
        run_performance_goal_risk,
        run_knowledge_answer,
        run_knowledge_generate,
        run_evaluate_outcome,
        run_prompt_refiner,
    ]

    # Cron jobs: prompt refiner runs every 6h
    cron_jobs = [
        cron(run_prompt_refiner, hour={0, 6, 12, 18}, minute=0),
    ]

    # Lifecycle hooks
    on_startup = startup
    on_shutdown = shutdown

    # Redis connection
    redis_settings = _redis_settings()

    # Concurrency: process up to 10 jobs simultaneously
    # Each job is a coroutine so this does not block the event loop
    max_jobs = 10

    # Job timeout: kill any job that takes longer than 5 minutes
    job_timeout = 300  # seconds

    # Keep results in Redis for 1 hour after completion
    keep_result = 3600  # seconds

    # Poll Redis every 0.5s for new jobs (low latency)
    poll_delay = 0.5  # seconds

    # Health check: log worker health every 60s
    health_check_interval = 60  # seconds
    health_check_key = b"arq:health-check"
