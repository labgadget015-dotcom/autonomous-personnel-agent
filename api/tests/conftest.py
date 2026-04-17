"""
conftest.py — Pytest fixtures for integration tests
=====================================================
Provides:
  - live_app: TestClient wired to a real Postgres DB (set via DATABASE_URL env)
  - db_conn:  raw psycopg2 connection for schema assertions
  - mock_app: TestClient with all LLM calls mocked (no OpenAI key needed)
"""

import os
import json
import pytest
import psycopg2
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# ---- Ensure test env is set before importing the app ----
os.environ.setdefault("API_TOKEN",    "test-token-ci")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-key-for-ci")
os.environ.setdefault("ENV", "development")

# DATABASE_URL must be injected by CI (set to the Postgres service URL)
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://testuser:testpass@localhost:5432/personnel_agent_test",
)


# ─────────────────────────────────────────────────
# Shared mock LLM response factory
# ─────────────────────────────────────────────────

def make_mock_llm_response(content: dict) -> MagicMock:
    """Return a MagicMock that looks like a LangChain ChatOpenAI response."""
    mock_response = MagicMock()
    mock_response.content = json.dumps(content)
    return mock_response


# ─────────────────────────────────────────────────
# FIXTURE: raw Postgres connection (for schema tests)
# ─────────────────────────────────────────────────

@pytest.fixture(scope="session")
def db_conn():
    """Session-scoped psycopg2 connection to the test database."""
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    conn.autocommit = True
    yield conn
    conn.close()


# ─────────────────────────────────────────────────
# FIXTURE: TestClient with mocked LLM (no API cost)
# ─────────────────────────────────────────────────

@pytest.fixture(scope="session")
def mock_app():
    """
    FastAPI TestClient with all LangChain LLM calls patched.
    Used for endpoint response-shape tests — no OpenAI calls made.
    """
    # Mock responses for each agent endpoint
    MOCK_RESPONSES = {
        "orchestrator": {
            "task_id": "test-task-001",
            "task_type": "scheduling",
            "sub_agents": ["SchedulingAgent"],
            "priority": "medium",
            "action_tier": "auto_execute",
            "reasoning": "Meeting summary request detected.",
            "plan": [{"step": 1, "agent": "SchedulingAgent", "action": "summarise_meeting", "payload": {}}],
            "needs_human_context": False,
            "human_question": None,
        },
        "talent_screen": {
            "candidate_id": "cand-001",
            "role_id": "role-001",
            "fit_score": 8.2,
            "fit_reasons": ["Strong Python background", "LLM experience"],
            "red_flags": [],
            "recommendation": "advance",
            "confidence": 0.87,
            "proposed_stage": "screened",
            "outreach_draft": "Hi, we'd love to connect...",
            "notes": "Excellent candidate",
            "bias_check_passed": True,
            "bias_notes": "No bias indicators detected",
        },
        "talent_outreach": {
            "subject": "Exciting opportunity at Gadget Lab",
            "body": "Hi there, we came across your profile...",
            "cta": "Would you be open to a 20-minute call?",
            "personalisation_notes": "Referenced GitHub projects",
        },
        "scheduling_summarise": {
            "summary": "Team synced on Q2 roadmap priorities.",
            "key_decisions": ["Prioritise LLM integration"],
            "commitments": [{"owner": "alice@example.com", "commitment": "Draft spec by Friday", "due_date": "2026-04-24"}],
            "action_items": [{"title": "Draft LLM spec", "owner": "alice@example.com", "due_at": "2026-04-24", "priority": "high"}],
            "follow_up_tasks": [],
            "sentiment": "positive",
            "risks": [],
        },
        "scheduling_followups": {
            "cold_contacts": [
                {"person_id": "p-001", "name": "Bob Smith", "days_cold": 18, "priority": 8,
                 "recommended_action": "email", "draft_message": "Hi Bob, just checking in...",
                 "reason_to_reconnect": "Q2 project kickoff"},
            ],
            "action_items": [{"person_id": "p-001", "task": "Email Bob", "due_at": "2026-04-20"}],
        },
        "scheduling_invite": {
            "subject": "Intro call — Personnel Agent discussion",
            "agenda": ["Introductions", "Project overview", "Next steps"],
            "body": "Hi, I'd love to connect...",
            "duration_minutes": 30,
            "suggested_slots": ["2026-04-20T10:00:00Z", "2026-04-21T14:00:00Z"],
            "preparation_notes": "Please review the project brief before the call.",
        },
        "onboarding_plan": {
            "welcome_email_subject": "Welcome to the team!",
            "welcome_email_body": "We're thrilled to have you...",
            "checklist": [
                {"item": "Send welcome email", "owner": "agent:onboarding",
                 "due_offset_days": 0, "category": "communication", "done": False},
                {"item": "Grant GitHub access", "owner": "human:hiring_manager",
                 "due_offset_days": 1, "category": "access", "done": False},
            ],
            "day_1_priorities": ["Review project brief", "Join Slack channels"],
            "week_1_goals": ["Complete onboarding checklist", "Meet the team"],
            "intro_calls": [{"with": "hiring manager", "purpose": "Welcome and orientation", "due_offset_days": 2}],
            "access_tasks": [{"system": "GitHub", "level": "contributor", "owner": "human:hiring_manager"}],
            "notes": "Remote-first onboarding",
        },
        "performance_brief": {
            "week_ending": "2026-04-19",
            "top_people_to_watch": [
                {"person_id": "p-001", "name": "Alice Dev", "reason": "Sprint velocity high",
                 "risk_level": "low", "recommended_action": "celebrate", "draft_message": "Great work this week!"},
            ],
            "at_risk_goals": [],
            "overloaded_people": [],
            "underloaded_people": [],
            "team_health_score": 8.5,
            "summary_paragraph": "Team performing well across all metrics.",
        },
        "knowledge_answer": {
            "question": "What is the remote work policy?",
            "answer": "All contractors may work remotely from any location.",
            "sources_cited": ["Remote Work Policy v2"],
            "confidence": 0.92,
            "caveats": "Policy subject to change",
            "follow_up_suggested": False,
            "follow_up_question": None,
        },
        "guardrails": {
            "passed": True,
            "risk_level": "low",
            "action_tier": "auto_execute",
            "escalation_path": "auto_log",
            "reason": "No PII, critical keywords, or policy flags detected.",
            "pii_found": [],
            "keyword_flags": [],
            "policy_flags": [],
            "injection_detected": False,
            "sanitised_text": "This is safe text.",
        },
    }

    with patch("langchain_openai.ChatOpenAI") as mock_chat:
        # Make every invoke() return the orchestrator mock by default
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = make_mock_llm_response(MOCK_RESPONSES["orchestrator"])
        mock_chat.return_value = mock_instance

        from main import app
        client = TestClient(app, raise_server_exceptions=True)

        # Store mock responses on client for per-test patching
        client._mock_responses = MOCK_RESPONSES
        client._mock_instance  = mock_instance

        yield client


# ─────────────────────────────────────────────────
# FIXTURE: TestClient against real DB (for schema + health tests)
# ─────────────────────────────────────────────────

@pytest.fixture(scope="session")
def live_app():
    """TestClient connected to the real Postgres service (CI only)."""
    os.environ["DATABASE_URL"] = DATABASE_URL
    with patch("langchain_openai.ChatOpenAI") as mock_chat:
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = make_mock_llm_response({"status": "ok"})
        mock_chat.return_value = mock_instance
        from main import app
        yield TestClient(app, raise_server_exceptions=True)


AUTH_HEADERS = {"x-api-token": "test-token-ci"}
