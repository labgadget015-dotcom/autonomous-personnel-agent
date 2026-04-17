"""
worker.py — arq Task Queue Worker
===================================
Offloads long-running agent calls to a Redis-backed async worker pool,
preventing HTTP gateway timeouts (30s) on multi-hop LLM chains that can
take 5–15 seconds per call.

Architecture:
  1. API receives request → enqueues job → returns {job_id, status: "queued"}
  2. Worker picks up job → runs agent → stores result in Redis (TTL 1 hour)
  3. Caller polls GET /jobs/{job_id} → receives status + result when complete

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
"""

import os
import logging
from typing import Any, Dict, List, Optional

from arq.connections import RedisSettings

logger = logging.getLogger("personnel-agent.worker")

# OTel tracer — no-op if package not installed
try:
    from telemetry import get_tracer, configure_tracing
    _tracer = get_tracer("personnel-agent.worker")
except ImportError:
    from telemetry import _NoOpTracer  # type: ignore
    _tracer = _NoOpTracer()

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
# Worker startup / shutdown hooks
# ============================================================

async def startup(ctx: Dict[str, Any]) -> None:
    """Initialise shared resources — agents, OTel tracing, DB pool, logging."""
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
        configure_tracing()   # no app= in worker — instruments Redis + psycopg2 only
    except Exception as exc:
        logger.warning("OTel tracing not configured in worker: %s", exc)
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
        result = ctx["orchestrator"].route(payload)
        result["request_id"] = job_request_id
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
        return ctx["talent"].screen_candidate(candidate_profile, role, history)


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
        return ctx["talent"].draft_outreach(candidate, role, tone)


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
        return ctx["talent"].build_pipeline_summary(pipeline_data)


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
        return ctx["scheduling"].summarise_meeting(meeting_notes, attendees, context)


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
        return ctx["scheduling"].identify_cold_followups(people, threshold_days)


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
        return ctx["scheduling"].draft_meeting_invite(purpose, attendees, duration_minutes, context)


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
        return ctx["onboarding"].generate_onboarding_plan(person, role, tools, company_links)


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
        return ctx["onboarding"].check_onboarding_progress(plan, days_since_start)


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
        return ctx["onboarding"].generate_offboarding_plan(person, reason, last_day, access_to_revoke)


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
        return ctx["performance"].generate_weekly_brief(people, goals, recent_interactions, github_activity)


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
        return ctx["performance"].assess_goal_risk(goal, person, interactions)


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
        return ctx["knowledge"].answer_policy_question(question, raw_docs)


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
        return ctx["knowledge"].generate_document(doc_type, context)


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
