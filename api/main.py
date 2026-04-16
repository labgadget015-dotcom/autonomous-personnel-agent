"""
main.py — FastAPI Service for Autonomous Personnel Agent
=========================================================
Production-ready REST API wrapping the multi-agent system.

Endpoints:
  POST /route          — Orchestrator routing (classify and plan any event)
  POST /talent/screen  — Screen a candidate against a role
  POST /talent/outreach — Draft outreach email for a candidate
  POST /scheduling/summarise — Summarise a meeting and extract action items
  POST /scheduling/followups — Identify cold contacts needing follow-up
  POST /scheduling/invite    — Draft a meeting invitation
  POST /onboarding/plan      — Generate onboarding plan for a new collaborator
  POST /onboarding/check     — Check progress on an onboarding plan
  POST /performance/brief    — Generate weekly performance brief
  POST /performance/goal-risk — Assess risk on a specific goal
  POST /knowledge/answer     — Answer a policy/SOP question via RAG
  POST /knowledge/generate   — Generate an internal document
  POST /guardrails/evaluate  — Run guardrail checks on any text

  GET  /health         — Health check
  GET  /metrics        — Basic service metrics
"""

import os
import time
import uuid
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agents import (
    OrchestratorAgent,
    TalentAgent,
    SchedulingAgent,
    OnboardingAgent,
    PerformanceAgent,
    KnowledgeAgent,
)
from guardrails import evaluate as guardrails_evaluate, GuardrailResult

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("personnel-agent")

# ============================================================
# APP INIT
# ============================================================
app = FastAPI(
    title="Autonomous Personnel Agent API",
    description="Multi-agent system for autonomous personnel management (n8n-compatible)",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory metrics (replace with Prometheus in production)
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


# ============================================================
# MIDDLEWARE: Request logging + metrics
# ============================================================

@app.middleware("http")
async def request_middleware(request: Request, call_next):
    start = time.time()
    _metrics["requests_total"] += 1
    try:
        response = await call_next(request)
        duration_ms = int((time.time() - start) * 1000)
        logger.info(f"{request.method} {request.url.path} → {response.status_code} ({duration_ms}ms)")
        return response
    except Exception as e:
        _metrics["errors_total"] += 1
        logger.error(f"Unhandled error on {request.url.path}: {e}")
        raise


# ============================================================
# HEALTH & METRICS
# ============================================================

@app.get("/health", tags=["System"])
async def health():
    return {
        "status": "ok",
        "service": "autonomous-personnel-agent",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/metrics", tags=["System"])
async def metrics(_: str = Depends(verify_token)):
    return {
        **_metrics,
        "uptime_seconds": int(
            (datetime.now(timezone.utc) - datetime.fromisoformat(_metrics["started_at"])).total_seconds()
        ),
    }


# ============================================================
# ORCHESTRATOR
# ============================================================

@app.post("/route", tags=["Orchestrator"])
async def route_event(
    req: EventRequest,
    _: str = Depends(verify_token),
):
    """
    Route any personnel event to the correct specialist agent(s).
    Returns a structured plan for n8n to execute.
    """
    try:
        orch = OrchestratorAgent()
        plan = orch.route(req.model_dump())
        plan["request_id"] = str(uuid.uuid4())
        return plan
    except Exception as e:
        _metrics["errors_total"] += 1
        logger.exception("Orchestrator error")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# TALENT AGENT
# ============================================================

@app.post("/talent/screen", tags=["Talent"])
async def screen_candidate(
    req: CandidateScreenRequest,
    _: str = Depends(verify_token),
):
    """Screen a candidate against an open role."""
    try:
        agent = TalentAgent()
        return agent.screen_candidate(req.candidate_profile, req.role, req.history)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/talent/outreach", tags=["Talent"])
async def draft_outreach(
    req: OutreachRequest,
    _: str = Depends(verify_token),
):
    """Draft a personalised cold-outreach email for a candidate."""
    try:
        agent = TalentAgent()
        return agent.draft_outreach(req.candidate, req.role, req.tone)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/talent/pipeline-summary", tags=["Talent"])
async def pipeline_summary(
    pipeline_data: List[Dict],
    _: str = Depends(verify_token),
):
    """Summarise the state of all active recruiting pipelines."""
    try:
        agent = TalentAgent()
        return agent.build_pipeline_summary(pipeline_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# SCHEDULING AGENT
# ============================================================

@app.post("/scheduling/summarise", tags=["Scheduling"])
async def summarise_meeting(
    req: MeetingSummaryRequest,
    _: str = Depends(verify_token),
):
    """Summarise a meeting and extract action items / commitments."""
    try:
        agent = SchedulingAgent()
        return agent.summarise_meeting(req.meeting_notes, req.attendees, req.context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/scheduling/followups", tags=["Scheduling"])
async def identify_followups(
    req: FollowupsRequest,
    _: str = Depends(verify_token),
):
    """Identify cold relationships that need a follow-up."""
    try:
        agent = SchedulingAgent()
        return agent.identify_cold_followups(req.people, req.threshold_days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/scheduling/invite", tags=["Scheduling"])
async def draft_invite(
    req: MeetingInviteRequest,
    _: str = Depends(verify_token),
):
    """Draft a meeting invitation with agenda."""
    try:
        agent = SchedulingAgent()
        return agent.draft_meeting_invite(
            req.purpose, req.attendees, req.duration_minutes, req.context
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# ONBOARDING AGENT
# ============================================================

@app.post("/onboarding/plan", tags=["Onboarding"])
async def create_onboarding_plan(
    req: OnboardingPlanRequest,
    _: str = Depends(verify_token),
):
    """Generate a customised onboarding plan for a new collaborator."""
    try:
        agent = OnboardingAgent()
        return agent.generate_onboarding_plan(
            req.person, req.role, req.tools, req.company_links
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/onboarding/check", tags=["Onboarding"])
async def check_onboarding(
    req: OnboardingCheckRequest,
    _: str = Depends(verify_token),
):
    """Check progress on an active onboarding plan and flag overdue items."""
    try:
        agent = OnboardingAgent()
        return agent.check_onboarding_progress(req.plan, req.days_since_start)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/onboarding/offboarding", tags=["Onboarding"])
async def create_offboarding(
    req: OffboardingRequest,
    _: str = Depends(verify_token),
):
    """Generate an offboarding plan and checklist."""
    try:
        agent = OnboardingAgent()
        return agent.generate_offboarding_plan(
            req.person, req.reason, req.last_day, req.access_to_revoke
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# PERFORMANCE AGENT
# ============================================================

@app.post("/performance/brief", tags=["Performance"])
async def weekly_brief(
    req: PerformanceBriefRequest,
    _: str = Depends(verify_token),
):
    """Generate the weekly 'Top People to Watch' performance brief."""
    try:
        agent = PerformanceAgent()
        return agent.generate_weekly_brief(
            req.people, req.goals, req.recent_interactions, req.github_activity
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/performance/goal-risk", tags=["Performance"])
async def goal_risk(
    req: GoalRiskRequest,
    _: str = Depends(verify_token),
):
    """Assess whether a specific goal is at risk of being missed."""
    try:
        agent = PerformanceAgent()
        return agent.assess_goal_risk(req.goal, req.person, req.interactions)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# KNOWLEDGE AGENT
# ============================================================

@app.post("/knowledge/answer", tags=["Knowledge"])
async def answer_policy_question(
    req: KnowledgeAnswerRequest,
    _: str = Depends(verify_token),
):
    """Answer a policy or SOP question using the internal knowledge base."""
    try:
        agent = KnowledgeAgent(
            vectorstore_path=os.getenv("KNOWLEDGE_VECTORSTORE_PATH")
        )
        return agent.answer_policy_question(req.question, req.raw_docs)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/knowledge/generate", tags=["Knowledge"])
async def generate_document(
    req: DocumentGenerateRequest,
    _: str = Depends(verify_token),
):
    """Generate an internal document (SOP, policy, handbook section, etc.)."""
    try:
        agent = KnowledgeAgent()
        return agent.generate_document(req.doc_type, req.context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# GUARDRAILS
# ============================================================

@app.post("/guardrails/evaluate", tags=["Guardrails"])
async def evaluate_guardrails(
    req: GuardrailsRequest,
    _: str = Depends(verify_token),
):
    """
    Run the full guardrail stack on any text.
    Use this before sending agent outputs to external channels.
    """
    try:
        result: GuardrailResult = guardrails_evaluate(
            req.text, req.context, req.sender_history
        )
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
