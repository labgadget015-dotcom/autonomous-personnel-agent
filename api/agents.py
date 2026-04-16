"""
agents.py — Multi-Agent Personnel System
=========================================
Orchestrator + 5 Specialist Sub-Agents:

  1. OrchestratorAgent   — Chief of Staff; routes tasks to specialist agents
  2. TalentAgent         — Sourcing, screening, candidate pipeline
  3. SchedulingAgent     — Calendars, follow-ups, meeting management
  4. OnboardingAgent     — Checklists, access, welcome comms
  5. PerformanceAgent    — Goal tracking, nudges, weekly briefing
  6. KnowledgeAgent      — RAG over internal docs, policies, SOPs

All agents output structured JSON for deterministic routing in n8n.
"""

import json
import os
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

from langchain_openai import ChatOpenAI
from langchain.prompts import PromptTemplate
from langchain.chains import LLMChain
from langchain.memory import ConversationBufferWindowMemory
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings
from langchain.chains import RetrievalQA
from langchain_core.messages import SystemMessage, HumanMessage


# ============================================================
# SHARED LLM INSTANCES
# ============================================================

def get_orchestrator_llm() -> ChatOpenAI:
    """High-capability model for reasoning and planning."""
    return ChatOpenAI(
        model=os.getenv("ORCHESTRATOR_MODEL", "gpt-4.1"),
        temperature=0.1,
        max_tokens=2048,
        api_key=os.getenv("OPENAI_API_KEY"),
    )


def get_agent_llm() -> ChatOpenAI:
    """Efficient model for specialist agents (classification, extraction, drafting)."""
    return ChatOpenAI(
        model=os.getenv("AGENT_MODEL", "gpt-4.1-mini"),
        temperature=0.2,
        max_tokens=1536,
        api_key=os.getenv("OPENAI_API_KEY"),
    )


def safe_parse_json(raw: str, fallback_key: str = "raw_output") -> Dict:
    """Attempt JSON parse; return partial result on failure."""
    raw = raw.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {fallback_key: raw, "parse_error": True}


# ============================================================
# 1. ORCHESTRATOR AGENT — Chief of Staff
# ============================================================

ORCHESTRATOR_SYSTEM = """You are the Chief of Staff Orchestrator for a fully autonomous personnel management system.

Your job is to:
1. Receive any personnel-related event or request
2. Classify it into one or more task types
3. Assign it to the correct specialist agent(s)
4. Apply the autonomy tier (auto_execute | needs_approval | blocked)
5. Output a structured JSON plan

TASK TYPES:
- recruiting       → TalentAgent
- scheduling       → SchedulingAgent
- onboarding       → OnboardingAgent
- performance      → PerformanceAgent
- knowledge_query  → KnowledgeAgent
- relationship     → (direct action or SchedulingAgent)
- multi_agent      → spawn multiple agents

AUTONOMY TIERS:
- auto_execute     → Log and execute immediately (low-risk ops)
- needs_approval   → Send to human before acting (outreach, offers, rejections)
- blocked          → Propose only; never execute (hiring, firing, pay changes)

Always output valid JSON matching this schema:
{
  "task_id": "uuid-string",
  "task_type": "recruiting|scheduling|onboarding|performance|knowledge_query|relationship|multi_agent",
  "sub_agents": ["TalentAgent"],
  "priority": "low|medium|high|urgent",
  "action_tier": "auto_execute|needs_approval|blocked",
  "reasoning": "Brief explanation",
  "plan": [
    {"step": 1, "agent": "TalentAgent", "action": "screen_candidate", "payload": {}}
  ],
  "needs_human_context": false,
  "human_question": null
}
"""

class OrchestratorAgent:
    """Routes any personnel event to the correct specialist agent(s)."""

    def __init__(self):
        self.llm = get_orchestrator_llm()

    def route(self, event: Dict) -> Dict:
        """
        Args:
            event: {
                type: email|webhook|cron|manual,
                content: str,
                sender: str (optional),
                metadata: {}
            }
        Returns:
            Structured routing plan (JSON-serialisable dict)
        """
        messages = [
            SystemMessage(content=ORCHESTRATOR_SYSTEM),
            HumanMessage(content=f"""
New personnel event received.

Event type: {event.get('type', 'unknown')}
Sender: {event.get('sender', 'unknown')}
Content: {event.get('content', '')}
Metadata: {json.dumps(event.get('metadata', {}), indent=2)}

Timestamp: {datetime.now(timezone.utc).isoformat()}

Analyse this event and output a routing plan as JSON.
""")
        ]

        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result.setdefault("routed_at", datetime.now(timezone.utc).isoformat())
        return result


# ============================================================
# 2. TALENT AGENT — Recruiting and Candidate Pipeline
# ============================================================

TALENT_SYSTEM = """You are the Talent Agent for an autonomous recruiting pipeline.

Your responsibilities:
- Screen candidates against open roles
- Produce structured fit assessments with scores and red flags
- Draft personalised outreach sequences
- Maintain candidate status and advance pipeline stages
- Flag bias risks and edge cases for human review

Always output JSON. For candidate screening use this schema:
{
  "candidate_id": "...",
  "role_id": "...",
  "fit_score": 0-10,
  "fit_reasons": ["specific skill match", "..."],
  "red_flags": ["missing requirement", "..."],
  "recommendation": "advance|hold|reject",
  "confidence": 0.0-1.0,
  "proposed_stage": "sourced|outreach|screened|interview|offer|hired|rejected",
  "outreach_draft": "...(if advancing)",
  "notes": "...",
  "bias_check_passed": true|false,
  "bias_notes": "..."
}
"""

class TalentAgent:
    """Sources, screens, and manages the candidate pipeline."""

    def __init__(self):
        self.llm = get_agent_llm()

    def screen_candidate(
        self,
        candidate_profile: Dict,
        role: Dict,
        history: Optional[List[Dict]] = None,
    ) -> Dict:
        """
        Assess a candidate against a role.

        Args:
            candidate_profile: {name, email, skills, experience, github_url, notes}
            role: {title, required_skills, nice_to_have, budget_min, budget_max, description}
            history: Prior interactions with this candidate (optional)

        Returns:
            Structured fit assessment.
        """
        history_text = ""
        if history:
            history_text = "\n".join([
                f"- [{h.get('timestamp','?')}] {h.get('channel','?')}: {h.get('summary','')}"
                for h in history[-5:]
            ])

        messages = [
            SystemMessage(content=TALENT_SYSTEM),
            HumanMessage(content=f"""
Screen this candidate for the role below.

CANDIDATE:
{json.dumps(candidate_profile, indent=2)}

ROLE:
{json.dumps(role, indent=2)}

PRIOR INTERACTIONS:
{history_text or "None on record."}

Output a screening assessment as JSON.
""")
        ]

        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["screened_at"] = datetime.now(timezone.utc).isoformat()
        result["agent"] = "TalentAgent"
        return result

    def draft_outreach(
        self,
        candidate: Dict,
        role: Dict,
        tone: str = "professional_warm",
    ) -> Dict:
        """Draft a personalised cold-outreach email for a candidate."""
        messages = [
            SystemMessage(content=TALENT_SYSTEM),
            HumanMessage(content=f"""
Draft a personalised outreach email for this candidate regarding the role below.
Tone: {tone}

CANDIDATE:
{json.dumps(candidate, indent=2)}

ROLE:
{json.dumps(role, indent=2)}

Output JSON:
{{
  "subject": "...",
  "body": "...",
  "cta": "...",
  "personalisation_notes": "what made this feel personal"
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["drafted_at"] = datetime.now(timezone.utc).isoformat()
        result["agent"] = "TalentAgent"
        return result

    def build_pipeline_summary(self, pipeline_data: List[Dict]) -> Dict:
        """Summarise the state of the recruiting pipeline for all open roles."""
        messages = [
            SystemMessage(content=TALENT_SYSTEM),
            HumanMessage(content=f"""
Analyse this recruiting pipeline data and produce a concise summary.

PIPELINE DATA:
{json.dumps(pipeline_data, indent=2)}

Output JSON:
{{
  "total_open_roles": 0,
  "total_candidates": 0,
  "at_risk_roles": ["role title: reason"],
  "top_candidates": [{{"name": "...", "role": "...", "score": 0, "recommended_next": "..."}}],
  "action_items": ["..."],
  "summary_paragraph": "..."
}}
""")
        ]
        response = self.llm.invoke(messages)
        return safe_parse_json(response.content)


# ============================================================
# 3. SCHEDULING AGENT — Calendars and Follow-ups
# ============================================================

SCHEDULING_SYSTEM = """You are the Scheduling Agent for an autonomous personnel system.

Your responsibilities:
- Draft meeting invitations and calendar requests
- Summarise meetings and extract action items / commitments
- Identify who needs a follow-up and when
- Schedule recurring check-ins and performance reviews
- Never send meeting invites without a clear agenda

Always output structured JSON.
"""

class SchedulingAgent:
    """Manages calendars, follow-ups, and meeting lifecycle."""

    def __init__(self):
        self.llm = get_agent_llm()

    def summarise_meeting(
        self,
        meeting_notes: str,
        attendees: List[Dict],
        context: Optional[Dict] = None,
    ) -> Dict:
        """
        Summarise meeting notes, extract commitments, and schedule follow-ups.

        Returns:
            {summary, commitments, action_items, follow_up_tasks, sentiment}
        """
        messages = [
            SystemMessage(content=SCHEDULING_SYSTEM),
            HumanMessage(content=f"""
Summarise this meeting and extract all commitments and action items.

ATTENDEES:
{json.dumps(attendees, indent=2)}

MEETING NOTES:
{meeting_notes}

CONTEXT:
{json.dumps(context or {}, indent=2)}

Output JSON:
{{
  "summary": "2-3 sentence overview",
  "key_decisions": ["..."],
  "commitments": [{{"owner": "name or email", "commitment": "...", "due_date": "YYYY-MM-DD or null"}}],
  "action_items": [{{"title": "...", "owner": "...", "due_at": "...", "priority": "medium"}}],
  "follow_up_tasks": [{{"person_email": "...", "task": "...", "due_at": "..."}}],
  "sentiment": "positive|neutral|mixed|tense",
  "risks": ["any concerns flagged in meeting"]
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["processed_at"] = datetime.now(timezone.utc).isoformat()
        result["agent"] = "SchedulingAgent"
        return result

    def identify_cold_followups(
        self,
        people: List[Dict],
        threshold_days: int = 14,
    ) -> Dict:
        """Identify relationships that have gone cold and draft follow-up plans."""
        cold = [
            p for p in people
            if p.get("days_since_contact", 0) >= threshold_days
            and p.get("priority", 5) >= 6
        ]

        if not cold:
            return {"cold_contacts": [], "action_items": [], "summary": "No cold contacts found."}

        messages = [
            SystemMessage(content=SCHEDULING_SYSTEM),
            HumanMessage(content=f"""
These contacts have gone cold (no interaction in {threshold_days}+ days).
Draft a prioritised follow-up plan for each.

COLD CONTACTS:
{json.dumps(cold, indent=2)}

Output JSON:
{{
  "cold_contacts": [
    {{
      "person_id": "...",
      "name": "...",
      "days_cold": 0,
      "priority": 0,
      "recommended_action": "email|call|linkedin|no_action",
      "draft_message": "...",
      "reason_to_reconnect": "..."
    }}
  ],
  "action_items": [{{"person_id": "...", "task": "...", "due_at": "YYYY-MM-DD"}}]
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["agent"] = "SchedulingAgent"
        return result

    def draft_meeting_invite(
        self,
        purpose: str,
        attendees: List[Dict],
        duration_minutes: int = 30,
        context: Optional[Dict] = None,
    ) -> Dict:
        """Draft a professional meeting invitation with agenda."""
        messages = [
            SystemMessage(content=SCHEDULING_SYSTEM),
            HumanMessage(content=f"""
Draft a meeting invitation for the following purpose.

PURPOSE: {purpose}
DURATION: {duration_minutes} minutes
ATTENDEES: {json.dumps(attendees)}
CONTEXT: {json.dumps(context or {})}

Output JSON:
{{
  "subject": "...",
  "agenda": ["agenda item 1", "..."],
  "body": "...",
  "duration_minutes": {duration_minutes},
  "suggested_slots": ["ISO datetime slot 1", "ISO datetime slot 2"],
  "preparation_notes": "what attendees should bring/review"
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["agent"] = "SchedulingAgent"
        return result


# ============================================================
# 4. ONBOARDING AGENT
# ============================================================

ONBOARDING_SYSTEM = """You are the Onboarding Agent for an autonomous personnel system.

Your responsibilities:
- Generate customised onboarding checklists based on role, location, and tech stack
- Draft welcome emails with the right tone and relevant links
- Create tasks for access provisioning, intro calls, first project briefs
- Track onboarding progress and send nudges if items are overdue
- Produce offboarding plans when collaborators exit

Output structured JSON for all responses.
"""

class OnboardingAgent:
    """Generates and tracks onboarding/offboarding plans."""

    def __init__(self):
        self.llm = get_agent_llm()

    def generate_onboarding_plan(
        self,
        person: Dict,
        role: Dict,
        tools: List[str],
        company_links: Optional[Dict] = None,
    ) -> Dict:
        """
        Generate a full onboarding plan for a new collaborator.

        Args:
            person: {name, email, role, timezone, country}
            role: {title, type, start_date, team, hiring_manager}
            tools: ["github", "slack", "notion", "vercel", ...]
            company_links: {handbook: url, notion: url, slack: url}

        Returns:
            Structured onboarding plan with checklist.
        """
        messages = [
            SystemMessage(content=ONBOARDING_SYSTEM),
            HumanMessage(content=f"""
Generate a complete onboarding plan for this new collaborator.

PERSON:
{json.dumps(person, indent=2)}

ROLE:
{json.dumps(role, indent=2)}

TOOLS TO PROVISION: {', '.join(tools)}

COMPANY LINKS:
{json.dumps(company_links or {}, indent=2)}

Output JSON:
{{
  "welcome_email_subject": "...",
  "welcome_email_body": "...",
  "checklist": [
    {{
      "item": "...",
      "owner": "agent:onboarding|human:hiring_manager|person",
      "due_offset_days": 0,
      "category": "access|communication|documentation|meeting|project",
      "done": false
    }}
  ],
  "day_1_priorities": ["..."],
  "week_1_goals": ["..."],
  "intro_calls": [
    {{"with": "hiring manager", "purpose": "...", "due_offset_days": 2}}
  ],
  "access_tasks": [{{"system": "...", "level": "...", "owner": "..."}}],
  "notes": "..."
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["agent"] = "OnboardingAgent"
        return result

    def generate_offboarding_plan(
        self,
        person: Dict,
        reason: str,
        last_day: str,
        access_to_revoke: List[str],
    ) -> Dict:
        """Generate an offboarding checklist."""
        messages = [
            SystemMessage(content=ONBOARDING_SYSTEM),
            HumanMessage(content=f"""
Generate an offboarding plan for this departing collaborator.

PERSON: {json.dumps(person)}
REASON: {reason}
LAST DAY: {last_day}
ACCESS TO REVOKE: {', '.join(access_to_revoke)}

Output JSON:
{{
  "farewell_email_draft": "...",
  "checklist": [
    {{"item": "...", "owner": "...", "due_date": "...", "done": false}}
  ],
  "knowledge_transfer_tasks": ["..."],
  "access_revocation": [{{"system": "...", "action": "...", "due_date": "..."}}],
  "exit_interview_recommended": true|false,
  "notes": "..."
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["agent"] = "OnboardingAgent"
        return result

    def check_onboarding_progress(
        self,
        plan: Dict,
        days_since_start: int,
    ) -> Dict:
        """Check onboarding plan progress and flag overdue items."""
        messages = [
            SystemMessage(content=ONBOARDING_SYSTEM),
            HumanMessage(content=f"""
Review this onboarding plan and identify overdue or at-risk items.
Days since start: {days_since_start}

PLAN:
{json.dumps(plan, indent=2)}

Output JSON:
{{
  "on_track": true|false,
  "completion_percent": 0-100,
  "overdue_items": ["{item_name}"],
  "at_risk_items": ["..."],
  "nudges_to_send": [{{"to": "email_or_role", "message": "..."}}],
  "overall_assessment": "..."
}}
""")
        ]
        response = self.llm.invoke(messages)
        return safe_parse_json(response.content)


# ============================================================
# 5. PERFORMANCE AGENT
# ============================================================

PERFORMANCE_SYSTEM = """You are the Performance Agent for an autonomous personnel system.

Your responsibilities:
- Track goals and deliverables per active collaborator
- Identify at-risk goals (overdue, stalled, or scope-creeping)
- Generate a weekly "Top People to Watch" brief
- Draft nudge messages for collaborators who need a check-in
- Summarise team health and flag over/under-loaded people

Output structured JSON. Never make final performance ratings — propose only.
"""

class PerformanceAgent:
    """Monitors goals, deliverables, and team health."""

    def __init__(self):
        self.llm = get_agent_llm()

    def generate_weekly_brief(
        self,
        people: List[Dict],
        goals: List[Dict],
        recent_interactions: List[Dict],
        github_activity: Optional[Dict] = None,
    ) -> Dict:
        """
        Generate the weekly "Top People to Watch" brief.

        Returns:
            {top_people, risk_flags, recommended_actions, summary}
        """
        messages = [
            SystemMessage(content=PERFORMANCE_SYSTEM),
            HumanMessage(content=f"""
Generate a weekly performance brief based on the data below.

ACTIVE PEOPLE (collaborators/contractors):
{json.dumps(people[:20], indent=2)}

CURRENT GOALS:
{json.dumps(goals[:30], indent=2)}

RECENT INTERACTIONS (last 7 days):
{json.dumps(recent_interactions[:20], indent=2)}

GITHUB ACTIVITY (commits, PRs, issues — last 7 days):
{json.dumps(github_activity or {}, indent=2)}

Output JSON:
{{
  "week_ending": "YYYY-MM-DD",
  "top_people_to_watch": [
    {{
      "person_id": "...",
      "name": "...",
      "reason": "...",
      "risk_level": "low|medium|high",
      "recommended_action": "check_in|nudge|escalate|celebrate",
      "draft_message": "..."
    }}
  ],
  "at_risk_goals": [
    {{
      "goal_id": "...",
      "person_name": "...",
      "goal_title": "...",
      "risk_reason": "...",
      "suggested_action": "..."
    }}
  ],
  "overloaded_people": ["name: reason"],
  "underloaded_people": ["name: reason"],
  "team_health_score": 0-10,
  "summary_paragraph": "..."
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["agent"] = "PerformanceAgent"
        return result

    def assess_goal_risk(self, goal: Dict, person: Dict, interactions: List[Dict]) -> Dict:
        """Assess whether a specific goal is at risk of being missed."""
        messages = [
            SystemMessage(content=PERFORMANCE_SYSTEM),
            HumanMessage(content=f"""
Assess whether this goal is on track or at risk.

GOAL:
{json.dumps(goal, indent=2)}

PERSON:
{json.dumps(person, indent=2)}

RECENT INTERACTIONS (context):
{json.dumps(interactions[-5:], indent=2)}

Output JSON:
{{
  "on_track": true|false,
  "risk_level": "low|medium|high",
  "risk_factors": ["..."],
  "days_to_deadline": 0,
  "recommended_action": "no_action|nudge|scope_adjustment|escalate",
  "draft_nudge": "...(if nudge recommended)",
  "confidence": 0.0-1.0
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["agent"] = "PerformanceAgent"
        return result


# ============================================================
# 6. KNOWLEDGE AGENT — RAG over Internal Docs
# ============================================================

KNOWLEDGE_SYSTEM = """You are the Knowledge Agent for an internal personnel system.

Your responsibilities:
- Answer questions about company policies, SOPs, agreements, and handbooks
- Generate and update internal documents (SOPs, handbooks, onboarding guides)
- Identify policy gaps or outdated content
- Always cite sources for answers; never hallucinate policies

Always output structured JSON with clear source citations.
"""

class KnowledgeAgent:
    """RAG-powered internal knowledge base agent."""

    def __init__(self, vectorstore_path: Optional[str] = None):
        self.llm = get_agent_llm()
        self.vectorstore = None
        self.retriever = None

        if vectorstore_path and os.path.exists(vectorstore_path):
            try:
                embeddings = OpenAIEmbeddings(
                    model="text-embedding-3-small",
                    api_key=os.getenv("OPENAI_API_KEY"),
                )
                self.vectorstore = FAISS.load_local(
                    vectorstore_path, embeddings, allow_dangerous_deserialization=True
                )
                self.retriever = self.vectorstore.as_retriever(
                    search_kwargs={"k": 4}
                )
            except Exception as e:
                print(f"[KnowledgeAgent] Could not load vectorstore: {e}")

    def answer_policy_question(
        self,
        question: str,
        raw_docs: Optional[List[str]] = None,
    ) -> Dict:
        """
        Answer a policy/SOP question using RAG if available, or raw docs if provided.

        Args:
            question: The question to answer.
            raw_docs: Optional list of policy text chunks to include as context.

        Returns:
            {answer, sources, confidence, caveats}
        """
        context_text = ""
        if self.retriever:
            try:
                docs = self.retriever.invoke(question)
                context_text = "\n\n".join([d.page_content for d in docs])
            except Exception:
                pass

        if raw_docs:
            context_text = context_text + "\n\n" + "\n\n".join(raw_docs)

        messages = [
            SystemMessage(content=KNOWLEDGE_SYSTEM),
            HumanMessage(content=f"""
Answer this question using the policy documents provided below.
If the answer is not in the documents, say so clearly.

QUESTION: {question}

POLICY DOCUMENTS:
{context_text or "No documents available in knowledge base yet. Answer from general HR best practice only."}

Output JSON:
{{
  "question": "{question}",
  "answer": "...",
  "sources_cited": ["document name or section"],
  "confidence": 0.0-1.0,
  "caveats": "...(any limitations or areas of uncertainty)",
  "follow_up_suggested": true|false,
  "follow_up_question": "..."
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["agent"] = "KnowledgeAgent"
        result["answered_at"] = datetime.now(timezone.utc).isoformat()
        return result

    def generate_document(
        self,
        doc_type: str,
        context: Dict,
    ) -> Dict:
        """
        Generate an internal document (SOP, handbook section, agreement draft, etc.)

        Args:
            doc_type: e.g. "sop", "onboarding_guide", "remote_work_policy"
            context: Custom inputs (role, company name, tools, etc.)
        """
        messages = [
            SystemMessage(content=KNOWLEDGE_SYSTEM),
            HumanMessage(content=f"""
Generate a professional internal document of type: {doc_type}

CONTEXT:
{json.dumps(context, indent=2)}

Output JSON:
{{
  "doc_type": "{doc_type}",
  "title": "...",
  "sections": [
    {{"heading": "...", "content": "..."}}
  ],
  "version": "1.0",
  "effective_date": "YYYY-MM-DD",
  "review_date": "YYYY-MM-DD",
  "notes": "Customise before publishing"
}}
""")
        ]
        response = self.llm.invoke(messages)
        result = safe_parse_json(response.content)
        result["agent"] = "KnowledgeAgent"
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        return result


# ============================================================
# AGENT REGISTRY
# ============================================================

AGENT_REGISTRY = {
    "orchestrator": OrchestratorAgent,
    "talent":       TalentAgent,
    "scheduling":   SchedulingAgent,
    "onboarding":   OnboardingAgent,
    "performance":  PerformanceAgent,
    "knowledge":    KnowledgeAgent,
}


def get_agent(name: str, **kwargs) -> Any:
    """Instantiate a named agent."""
    cls = AGENT_REGISTRY.get(name)
    if not cls:
        raise ValueError(f"Unknown agent: {name}. Available: {list(AGENT_REGISTRY.keys())}")
    return cls(**kwargs)
