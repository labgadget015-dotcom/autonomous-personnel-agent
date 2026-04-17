"""
main.py — FastAPI Service for Autonomous Personnel Agent
=========================================================
Production-ready REST API wrapping the multi-agent system.

Enhancements in this version:
  - Structured JSON logging (structlog) on every request
  - Request ID middleware — UUID per request, echoed in X-Request-ID header
  - Per-token rate limiting (slowapi + Redis) — 20 req/min on agent routes
  - LLM token cost tracking callback — per-agent token/USD counters at /metrics
  - arq async job queue — POST /async/* returns {job_id} immediately;
    GET /jobs/{job_id} polls result (prevents 30s gateway timeouts)
  - Webhook retry helper available via webhooks.post_webhook

Synchronous endpoints (kept for backwards-compat with existing n8n nodes):
  POST /route, /talent/*, /scheduling/*, /onboarding/*, /performance/*,
  /knowledge/*, /guardrails/*

Async endpoints (new — for long-running calls):
  POST /async/route
  POST /async/talent/screen
  POST /async/talent/outreach
  POST /async/talent/pipeline-summary
  POST /async/scheduling/summarise
  POST /async/scheduling/followups
  POST /async/scheduling/invite
  POST /async/onboarding/plan
  POST /async/onboarding/check
  POST /async/onboarding/offboarding
  POST /async/performance/brief
  POST /async/performance/goal-risk
  POST /async/knowledge/answer
  POST /async/knowledge/generate
  GET  /jobs/{job_id}    — Poll job status + result

System:
  GET  /health           — Deep health check (Postgres, LLM key probe, env, disk)
  GET  /health/live      — Liveness probe (always 200)
  GET  /metrics          — Token cost + request counters
"""

import os
import time
import uuid
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---- Structured logging (configure before any other imports log) ----
from logging_config import configure_logging, get_logger
configure_logging()
log = get_logger("personnel-agent")

from agents import (
    OrchestratorAgent,
    TalentAgent,
    SchedulingAgent,
    OnboardingAgent,
    PerformanceAgent,
    KnowledgeAgent,
)
from guardrails import evaluate as guardrails_evaluate, GuardrailResult
from health import run_health_checks
from middleware.request_id import RequestIDMiddleware
from middleware.cost_tracker import CostTrackingCallback, get_cumulative_cost

# ---- Rate limiting (requires Redis — soft-fail if unavailable) ----
try:
    from slowapi.errors import RateLimitExceeded
    from middleware.rate_limit import limiter, rate_limit_exceeded_handler, AGENT_RATE_LIMIT
    _rate_limit_available = True
except ImportError:
    _rate_limit_available = False
    AGENT_RATE_LIMIT = "20/minute"

# ---- arq job queue (requires Redis — soft-fail if unavailable) ----
_arq_pool = None

async def _get_arq_pool():
    """Return a shared arq Redis pool, initialised on first call."""
    global _arq_pool
    if _arq_pool is None:
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
            from urllib.parse import urlparse
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            parsed = urlparse(redis_url)
            settings = RedisSettings(
                host=parsed.hostname or "localhost",
                port=parsed.port or 6379,
                password=parsed.password or None,
            )
            _arq_pool = await create_pool(settings)
            log.info("arq_pool_connected", redis_url=redis_url)
        except Exception as exc:
            log.warning("arq_pool_unavailable", error=str(exc),
                       detail="Async /async/* endpoints will return 503")
    return _arq_pool


# ============================================================
# APP LIFECYCLE
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise arq pool on startup; close on shutdown."""
    log.info("service_starting", service="autonomous-personnel-agent")
    pool = await _get_arq_pool()
    if pool:
        app.state.arq_pool = pool
    yield
    log.info("service_stopping")
    if _arq_pool:
        await _arq_pool.aclose()


# ============================================================
# APP INIT
# ============================================================

app = FastAPI(
    title="Autonomous Personnel Agent API",
    description=(
        "Multi-agent system for autonomous personnel management (n8n-compatible). "
        "Use POST /async/* for long-running agent calls; poll GET /jobs/{job_id} for results."
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ---- Middleware: order matters — registered in reverse call order ----
# 1. Request ID (outermost — assigns ID before anything logs)
app.add_middleware(RequestIDMiddleware)

# 2. CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Rate limiting
if _rate_limit_available:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# ---- In-memory metrics ----
_metrics: Dict[str, Any] = {
    "requests_total": 0,
    "errors_total": 0,
    "started_at": datetime.now(timezone.utc).isoformat(),
}

# ============================================================
# AUTH
# ============================================================

API_TOKEN = os.getenv("API_TOKEN", "change-me-in-production")


def verify_token(x_api_token: str = Header(...)) -> str:
    if x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token")
    return x_api_token


# ============================================================
# REQUEST / RESPONSE MODELS
# ============================================================

class EventRequest(BaseModel):
    type: str = Field(..., description="email|webhook|cron|manual|slack")
    content: str = Field(..., description="Raw content / body of the event")
    sender: Optional[str] = Field(None, description="Sender email or identifier")
    metadata: Dict = Field(default_factory=dict)


class CandidateScreenRequest(BaseModel):
    candidate_profile: Dict = Field(..., description="{name, email, skills, experience, notes}")
    role: Dict = Field(..., description="{title, required_skills, nice_to_have, description}")
    history: Optional[List[Dict]] = Field(None, description="Prior interactions")


class OutreachRequest(BaseModel):
    candidate: Dict
    role: Dict
    tone: str = Field("professional_warm", description="professional_warm|formal|casual")


class MeetingSummaryRequest(BaseModel):
    meeting_notes: str
    attendees: List[Dict]
    context: Optional[Dict] = None


class FollowupsRequest(BaseModel):
    people: List[Dict] = Field(..., description="People records with days_since_contact and priority")
    threshold_days: int = Field(14, description="Days without contact to be considered cold")


class MeetingInviteRequest(BaseModel):
    purpose: str
    attendees: List[Dict]
    duration_minutes: int = 30
    context: Optional[Dict] = None


class OnboardingPlanRequest(BaseModel):
    person: Dict = Field(..., description="{name, email, role, timezone, country}")
    role: Dict = Field(..., description="{title, type, start_date, team, hiring_manager}")
    tools: List[str] = Field(..., description="Tools to provision, e.g. ['github', 'slack']")
    company_links: Optional[Dict] = None


class OnboardingCheckRequest(BaseModel):
    plan: Dict
    days_since_start: int


class OffboardingRequest(BaseModel):
    person: Dict
    reason: str
    last_day: str
    access_to_revoke: List[str]


class PerformanceBriefRequest(BaseModel):
    people: List[Dict]
    goals: List[Dict]
    recent_interactions: List[Dict]
    github_activity: Optional[Dict] = None


class GoalRiskRequest(BaseModel):
    goal: Dict
    person: Dict
    interactions: List[Dict]


class KnowledgeAnswerRequest(BaseModel):
    question: str
    raw_docs: Optional[List[str]] = None


class DocumentGenerateRequest(BaseModel):
    doc_type: str = Field(..., description="sop|handbook|onboarding_guide|policy|agreement")
    context: Dict


class GuardrailsRequest(BaseModel):
    text: str
    context: Optional[Dict] = None
    sender_history: Optional[Dict] = None


class AsyncJobResponse(BaseModel):
    job_id: str
    status: str = "queued"
    message: str = "Job enqueued. Poll GET /jobs/{job_id} for result."


# ============================================================
# MIDDLEWARE: Structured request logging + metrics
# ============================================================

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start = time.time()
    _metrics["requests_total"] += 1
    try:
        response = await call_next(request)
        latency_ms = int((time.time() - start) * 1000)
        log.info(
            "request_complete",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=latency_ms,
        )
        return response
    except Exception as exc:
        _metrics["errors_total"] += 1
        log.exception("unhandled_error", path=request.url.path, error=str(exc))
        raise


# ============================================================
# HELPERS
# ============================================================

def _cost_callback(agent: str) -> CostTrackingCallback:
    """Return a cost-tracking callback for the named agent."""
    return CostTrackingCallback(agent=agent)


async def _enqueue(func_name: str, request_id: str, **kwargs) -> AsyncJobResponse:
    """Enqueue a job on the arq worker pool. Raises 503 if Redis unavailable."""
    pool = await _get_arq_pool()
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="Async job queue unavailable. Redis is not connected. "
                   "Use the synchronous endpoint instead.",
        )
    job = await pool.enqueue_job(func_name, **kwargs, _job_id=request_id)
    return AsyncJobResponse(job_id=job.job_id)


# ============================================================
# HEALTH & METRICS
# ============================================================

@app.get("/health", tags=["System"])
async def health():
    response, http_status = run_health_checks()
    return JSONResponse(status_code=http_status, content=response)


@app.get("/health/live", tags=["System"])
async def liveness():
    from health import _SERVICE_START_WALL, BUILD_VERSION, BUILD_SHA
    return {
        "status": "ok",
        "service": "autonomous-personnel-agent",
        "version": "2.0.0",
        "build_version": BUILD_VERSION,
        "build_sha": BUILD_SHA,
        "started_at": _SERVICE_START_WALL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/metrics", tags=["System"])
async def metrics(_: str = Depends(verify_token)):
    cost_data = get_cumulative_cost()
    return {
        **_metrics,
        "uptime_seconds": int(
            (datetime.now(timezone.utc) - datetime.fromisoformat(_metrics["started_at"])).total_seconds()
        ),
        "llm_cost": cost_data,
    }


# ============================================================
# JOB POLLING
# ============================================================

@app.get("/jobs/{job_id}", tags=["AsyncJobs"])
async def get_job(job_id: str, _: str = Depends(verify_token)):
    """
    Poll the result of an async agent job.
    Returns status: queued | in_progress | complete | failed | not_found
    """
    pool = await _get_arq_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Job queue unavailable")
    try:
        from arq.jobs import Job, JobStatus
        job = Job(job_id, pool)
        status = await job.status()
        if status == JobStatus.not_found:
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        if status in (JobStatus.complete,):
            result = await job.result(timeout=0)
            return {"job_id": job_id, "status": "complete", "result": result}
        return {"job_id": job_id, "status": status.value}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================
# ORCHESTRATOR — sync + async
# ============================================================

@app.post("/route", tags=["Orchestrator"])
async def route_event(req: EventRequest, request: Request, _: str = Depends(verify_token)):
    try:
        orch = OrchestratorAgent()
        plan = orch.route(req.model_dump())
        plan["request_id"] = request.headers.get("x-request-id", str(uuid.uuid4()))
        log.info("orchestrator_routed", event_type=req.type)
        return plan
    except Exception as exc:
        _metrics["errors_total"] += 1
        log.exception("orchestrator_error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/route", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_route_event(req: EventRequest, request: Request, _: str = Depends(verify_token)):
    """Async version of /route — returns immediately with a job_id."""
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_route", rid, payload=req.model_dump())


# ============================================================
# TALENT AGENT — sync + async
# ============================================================

@app.post("/talent/screen", tags=["Talent"])
async def screen_candidate(req: CandidateScreenRequest, request: Request, _: str = Depends(verify_token)):
    try:
        agent = TalentAgent()
        result = agent.screen_candidate(req.candidate_profile, req.role, req.history)
        log.info("talent_screen_complete")
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/talent/screen", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_screen_candidate(req: CandidateScreenRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_talent_screen", rid,
                          candidate_profile=req.candidate_profile,
                          role=req.role, history=req.history)


@app.post("/talent/outreach", tags=["Talent"])
async def draft_outreach(req: OutreachRequest, _: str = Depends(verify_token)):
    try:
        agent = TalentAgent()
        return agent.draft_outreach(req.candidate, req.role, req.tone)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/talent/outreach", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_draft_outreach(req: OutreachRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_talent_outreach", rid,
                          candidate=req.candidate, role=req.role, tone=req.tone)


@app.post("/talent/pipeline-summary", tags=["Talent"])
async def pipeline_summary(pipeline_data: List[Dict], _: str = Depends(verify_token)):
    try:
        agent = TalentAgent()
        return agent.build_pipeline_summary(pipeline_data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/talent/pipeline-summary", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_pipeline_summary(pipeline_data: List[Dict], request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_talent_pipeline_summary", rid, pipeline_data=pipeline_data)


# ============================================================
# SCHEDULING AGENT — sync + async
# ============================================================

@app.post("/scheduling/summarise", tags=["Scheduling"])
async def summarise_meeting(req: MeetingSummaryRequest, _: str = Depends(verify_token)):
    try:
        agent = SchedulingAgent()
        return agent.summarise_meeting(req.meeting_notes, req.attendees, req.context)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/scheduling/summarise", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_summarise_meeting(req: MeetingSummaryRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_scheduling_summarise", rid,
                          meeting_notes=req.meeting_notes, attendees=req.attendees, context=req.context)


@app.post("/scheduling/followups", tags=["Scheduling"])
async def identify_followups(req: FollowupsRequest, _: str = Depends(verify_token)):
    try:
        agent = SchedulingAgent()
        return agent.identify_cold_followups(req.people, req.threshold_days)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/scheduling/followups", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_identify_followups(req: FollowupsRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_scheduling_followups", rid,
                          people=req.people, threshold_days=req.threshold_days)


@app.post("/scheduling/invite", tags=["Scheduling"])
async def draft_invite(req: MeetingInviteRequest, _: str = Depends(verify_token)):
    try:
        agent = SchedulingAgent()
        return agent.draft_meeting_invite(req.purpose, req.attendees, req.duration_minutes, req.context)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/scheduling/invite", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_draft_invite(req: MeetingInviteRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_scheduling_invite", rid, purpose=req.purpose,
                          attendees=req.attendees, duration_minutes=req.duration_minutes, context=req.context)


# ============================================================
# ONBOARDING AGENT — sync + async
# ============================================================

@app.post("/onboarding/plan", tags=["Onboarding"])
async def create_onboarding_plan(req: OnboardingPlanRequest, _: str = Depends(verify_token)):
    try:
        agent = OnboardingAgent()
        return agent.generate_onboarding_plan(req.person, req.role, req.tools, req.company_links)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/onboarding/plan", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_create_onboarding_plan(req: OnboardingPlanRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_onboarding_plan", rid, person=req.person, role=req.role,
                          tools=req.tools, company_links=req.company_links)


@app.post("/onboarding/check", tags=["Onboarding"])
async def check_onboarding(req: OnboardingCheckRequest, _: str = Depends(verify_token)):
    try:
        agent = OnboardingAgent()
        return agent.check_onboarding_progress(req.plan, req.days_since_start)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/onboarding/check", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_check_onboarding(req: OnboardingCheckRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_onboarding_check", rid, plan=req.plan, days_since_start=req.days_since_start)


@app.post("/onboarding/offboarding", tags=["Onboarding"])
async def create_offboarding(req: OffboardingRequest, _: str = Depends(verify_token)):
    try:
        agent = OnboardingAgent()
        return agent.generate_offboarding_plan(req.person, req.reason, req.last_day, req.access_to_revoke)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/onboarding/offboarding", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_create_offboarding(req: OffboardingRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_onboarding_offboarding", rid, person=req.person, reason=req.reason,
                          last_day=req.last_day, access_to_revoke=req.access_to_revoke)


# ============================================================
# PERFORMANCE AGENT — sync + async
# ============================================================

@app.post("/performance/brief", tags=["Performance"])
async def weekly_brief(req: PerformanceBriefRequest, _: str = Depends(verify_token)):
    try:
        agent = PerformanceAgent()
        return agent.generate_weekly_brief(req.people, req.goals, req.recent_interactions, req.github_activity)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/performance/brief", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_weekly_brief(req: PerformanceBriefRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_performance_brief", rid, people=req.people, goals=req.goals,
                          recent_interactions=req.recent_interactions, github_activity=req.github_activity)


@app.post("/performance/goal-risk", tags=["Performance"])
async def goal_risk(req: GoalRiskRequest, _: str = Depends(verify_token)):
    try:
        agent = PerformanceAgent()
        return agent.assess_goal_risk(req.goal, req.person, req.interactions)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/performance/goal-risk", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_goal_risk(req: GoalRiskRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_performance_goal_risk", rid,
                          goal=req.goal, person=req.person, interactions=req.interactions)


# ============================================================
# KNOWLEDGE AGENT — sync + async
# ============================================================

@app.post("/knowledge/answer", tags=["Knowledge"])
async def answer_policy_question(req: KnowledgeAnswerRequest, _: str = Depends(verify_token)):
    try:
        agent = KnowledgeAgent(vectorstore_path=os.getenv("KNOWLEDGE_VECTORSTORE_PATH"))
        return agent.answer_policy_question(req.question, req.raw_docs)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/knowledge/answer", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_answer_policy_question(req: KnowledgeAnswerRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_knowledge_answer", rid, question=req.question, raw_docs=req.raw_docs)


@app.post("/knowledge/generate", tags=["Knowledge"])
async def generate_document(req: DocumentGenerateRequest, _: str = Depends(verify_token)):
    try:
        agent = KnowledgeAgent()
        return agent.generate_document(req.doc_type, req.context)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/async/knowledge/generate", tags=["AsyncJobs"], response_model=AsyncJobResponse)
async def async_generate_document(req: DocumentGenerateRequest, request: Request, _: str = Depends(verify_token)):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    return await _enqueue("run_knowledge_generate", rid, doc_type=req.doc_type, context=req.context)


# ============================================================
# GUARDRAILS
# ============================================================

@app.post("/guardrails/evaluate", tags=["Guardrails"])
async def evaluate_guardrails(req: GuardrailsRequest, _: str = Depends(verify_token)):
    try:
        result: GuardrailResult = guardrails_evaluate(req.text, req.context, req.sender_history)
        return {
            "passed": result.passed,
            "risk_level": result.risk_level,
            "action_tier": result.action_tier,
            "escalation_path": result.escalation_path,
            "reason": result.reason,
            "pii_found": [{"type": t, "value": "***REDACTED***"} for t, _ in result.pii_findings],
            "keyword_flags": result.keyword_flags,
            "policy_flags": result.policy_flags,
            "injection_detected": result.injection_detected,
            "sanitised_text": result.sanitised_text,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=os.getenv("ENV", "production") == "development",
        log_level="info",
    )
