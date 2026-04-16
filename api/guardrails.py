"""
guardrails.py — Two-Layer Safety Architecture for Autonomous Personnel Agent

Layer 1: GUARDRAILS (fast, deterministic)
  - PII detection (regex + pattern matching)
  - Critical keyword detection
  - Policy violation flags
  - Prompt-injection detection

Layer 2: CONSTRAINTS (action-level)
  - Action tier classification (auto_execute / needs_approval / blocked)
  - Audit logging
  - Rate limiting helpers
"""

import re
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional


# ============================================================
# ENUMS
# ============================================================

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionTier(str, Enum):
    AUTO_EXECUTE = "auto_execute"       # Log and execute immediately
    NEEDS_APPROVAL = "needs_approval"   # Send to human for approval
    BLOCKED = "blocked"                 # Propose only — cannot execute


class EscalationPath(str, Enum):
    AUTO_LOG = "auto_log"
    HRBP_REVIEW = "hrbp_review"
    HR_LEAD = "hr_lead"
    LEGAL_URGENT = "legal_urgent"


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class GuardrailResult:
    passed: bool                          # True = safe to proceed
    risk_level: RiskLevel
    action_tier: ActionTier
    escalation_path: EscalationPath
    reason: str
    pii_findings: List[Tuple[str, str]] = field(default_factory=list)
    keyword_flags: List[str] = field(default_factory=list)
    policy_flags: List[str] = field(default_factory=list)
    injection_detected: bool = False
    sanitised_text: Optional[str] = None  # Text with PII masked


@dataclass
class ActionConstraintResult:
    allowed: bool
    action_tier: ActionTier
    reason: str
    requires_approval_from: Optional[str] = None   # e.g. "hr_lead", "owner"


# ============================================================
# CRITICAL KEYWORDS (immediate legal/safety escalation)
# ============================================================

CRITICAL_KEYWORDS: List[str] = [
    # Harassment / safety
    "harassment", "sexual harassment", "bullying", "intimidation",
    "physical threat", "violence", "assault", "stalking",
    # Legal exposure
    "discrimination", "retaliation", "whistleblower", "protected disclosure",
    "constructive dismissal", "unfair dismissal", "tribunal",
    # Security / fraud
    "data breach", "fraud", "bribery", "corruption", "money laundering",
    "insider threat", "unauthorized access",
    # Safeguarding
    "child", "minor", "abuse", "safeguarding"
]

HIGH_KEYWORDS: List[str] = [
    "grievance", "complaint", "hostile", "unsafe", "threatened",
    "inappropriate", "bias", "unfair", "illegal", "wage theft",
    "unpaid", "overwork", "burnout", "mental health", "sick leave denied"
]

# ============================================================
# PII PATTERNS
# ============================================================

PII_PATTERNS: Dict[str, str] = {
    "email":       r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    "phone_uk":    r"\b(?:0|\+44)[1-9]\d{8,9}\b",
    "phone_us":    r"\b\d{3}[\-.\s]?\d{3}[\-.\s]?\d{4}\b",
    "ni_number":   r"\b[A-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-Z]\b",  # UK NI
    "ssn":         r"\b\d{3}-\d{2}-\d{4}\b",                           # US SSN
    "passport":    r"\b[A-Z]{1,2}\d{6,9}\b",
    "iban":        r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7,19}\b",
    "credit_card": r"\b(?:\d{4}[\s\-]?){3}\d{4}\b",
    "dob":         r"\b(?:DOB|Date of Birth|born|birthday)[:\s]+\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b",
    "salary":      r"\b(?:salary|wage|pay|earning)[:\s]*[£$€]?\s*\d[\d,\.]*\b",
}

# ============================================================
# PROMPT INJECTION PATTERNS
# ============================================================

INJECTION_PATTERNS: List[str] = [
    r"ignore (previous|prior|all) instructions",
    r"disregard (previous|prior|your) (instructions|prompt|system)",
    r"you are now",
    r"act as (?!an? (HR|recruiter|assistant))",  # allow normal role-play framing
    r"jailbreak",
    r"DAN mode",
    r"pretend you (are|have no)",
    r"forget your (training|guidelines|restrictions)",
    r"execute (sql|bash|python|code|command)",
    r"run (the following|this) (code|script|query|command)",
    r"drop table",
    r"delete from",
    r"SELECT \* FROM",
    r"<script",
    r"javascript:",
    r"eval\(",
    r"__import__",
]

# ============================================================
# ACTION TIERS: What each agent can/cannot do autonomously
# ============================================================

# Maps action_type → ActionTier (can be overridden per context)
DEFAULT_ACTION_TIERS: Dict[str, ActionTier] = {
    # Auto-execute (no approval)
    "draft_email":              ActionTier.AUTO_EXECUTE,
    "update_record":            ActionTier.AUTO_EXECUTE,
    "create_record":            ActionTier.AUTO_EXECUTE,
    "generate_summary":         ActionTier.AUTO_EXECUTE,
    "generate_document":        ActionTier.AUTO_EXECUTE,
    "send_reminder":            ActionTier.AUTO_EXECUTE,
    "schedule_followup":        ActionTier.AUTO_EXECUTE,
    "create_task":              ActionTier.AUTO_EXECUTE,
    "log_interaction":          ActionTier.AUTO_EXECUTE,
    "send_scheduling_email":    ActionTier.AUTO_EXECUTE,
    "update_onboarding_status": ActionTier.AUTO_EXECUTE,

    # Needs approval
    "send_outreach_email":      ActionTier.NEEDS_APPROVAL,
    "send_offer":               ActionTier.NEEDS_APPROVAL,
    "shortlist_candidate":      ActionTier.NEEDS_APPROVAL,
    "flag_performance_risk":    ActionTier.NEEDS_APPROVAL,
    "propose_rate_change":      ActionTier.NEEDS_APPROVAL,
    "request_contract_change":  ActionTier.NEEDS_APPROVAL,
    "bulk_email":               ActionTier.NEEDS_APPROVAL,
    "archive_person":           ActionTier.NEEDS_APPROVAL,
    "send_rejection_email":     ActionTier.NEEDS_APPROVAL,

    # Blocked — propose only
    "hire_candidate":           ActionTier.BLOCKED,
    "terminate_person":         ActionTier.BLOCKED,
    "change_compensation":      ActionTier.BLOCKED,
    "issue_performance_rating": ActionTier.BLOCKED,
    "legal_action":             ActionTier.BLOCKED,
    "delete_record":            ActionTier.BLOCKED,
    "grant_system_access":      ActionTier.BLOCKED,
    "sign_contract":            ActionTier.BLOCKED,
}


# ============================================================
# CORE GUARDRAIL FUNCTIONS
# ============================================================

def extract_pii(text: str) -> List[Tuple[str, str]]:
    """Detect PII in text. Returns list of (pii_type, matched_value)."""
    findings = []
    for pii_type, pattern in PII_PATTERNS.items():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            findings.append((pii_type, match.group()))
    return findings


def mask_pii(text: str) -> str:
    """Replace PII with masked placeholders."""
    for pii_type, pattern in PII_PATTERNS.items():
        placeholder = f"[REDACTED:{pii_type.upper()}]"
        text = re.sub(pattern, placeholder, text, flags=re.IGNORECASE)
    return text


def detect_critical_keywords(text: str) -> List[str]:
    """Identify critical escalation-triggering keywords."""
    found = []
    text_lower = text.lower()
    for kw in CRITICAL_KEYWORDS:
        if kw in text_lower:
            found.append(kw)
    return found


def detect_high_keywords(text: str) -> List[str]:
    """Identify high-risk (but not critical) keywords."""
    found = []
    text_lower = text.lower()
    for kw in HIGH_KEYWORDS:
        if kw in text_lower:
            found.append(kw)
    return found


def detect_injection(text: str) -> bool:
    """Check for prompt injection attempts."""
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def check_policy_flags(text: str) -> List[str]:
    """Cross-reference text against known policy trigger phrases."""
    flags = []
    text_lower = text.lower()

    policy_checks = [
        ("working time directive", ["working hours", "overtime", "rest period", "working time"]),
        ("wage / payment issue", ["unpaid", "wage", "underpaid", "pay withheld"]),
        ("pregnancy / maternity", ["maternity", "pregnancy", "pregnant", "parental leave"]),
        ("long covid / disability", ["long covid", "chronic illness", "disability", "reasonable adjustment"]),
        ("flexible working rights (ERA 2023)", ["flexible working", "hybrid working", "remote request denied"]),
        ("GDPR / data rights", ["personal data", "gdpr", "data request", "subject access"]),
        ("whistleblowing / protected disclosure", ["whistleblow", "protected disclosure", "reported to regulator"]),
    ]

    for policy_name, keywords in policy_checks:
        if any(kw in text_lower for kw in keywords):
            flags.append(policy_name)

    return flags


# ============================================================
# MAIN GUARDRAIL EVALUATION
# ============================================================

def evaluate(
    text: str,
    context: Optional[Dict] = None,
    sender_history: Optional[Dict] = None,
) -> GuardrailResult:
    """
    Main entry point. Evaluate text through all guardrail layers.

    Args:
        text: The raw input text (email body, complaint, agent output, etc.)
        context: Optional context dict (channel, agent_name, action_type, etc.)
        sender_history: Prior complaint count, flags, manager notes.

    Returns:
        GuardrailResult with risk level, action tier, escalation path, and details.
    """
    context = context or {}
    sender_history = sender_history or {}

    # --- Injection check (always first)
    injection = detect_injection(text)
    if injection:
        return GuardrailResult(
            passed=False,
            risk_level=RiskLevel.CRITICAL,
            action_tier=ActionTier.BLOCKED,
            escalation_path=EscalationPath.LEGAL_URGENT,
            reason="Prompt injection pattern detected. Input blocked.",
            injection_detected=True,
            sanitised_text=mask_pii(text),
        )

    # --- PII detection
    pii_findings = extract_pii(text)
    sanitised = mask_pii(text)

    # --- Keyword checks
    critical_kw = detect_critical_keywords(text)
    high_kw = detect_high_keywords(text)
    policy_flags = check_policy_flags(text)

    prior_complaints = sender_history.get("prior_complaints_count", 0)

    # --- CRITICAL: Immediate bypass to legal
    if critical_kw:
        return GuardrailResult(
            passed=False,
            risk_level=RiskLevel.CRITICAL,
            action_tier=ActionTier.BLOCKED,
            escalation_path=EscalationPath.LEGAL_URGENT,
            reason=f"Critical keywords detected: {', '.join(critical_kw)}. Escalated to Legal.",
            pii_findings=pii_findings,
            keyword_flags=critical_kw,
            policy_flags=policy_flags,
            sanitised_text=sanitised,
        )

    # --- HIGH: PII + policy violation, or repeat complainant + flags
    if pii_findings and policy_flags:
        return GuardrailResult(
            passed=False,
            risk_level=RiskLevel.HIGH,
            action_tier=ActionTier.NEEDS_APPROVAL,
            escalation_path=EscalationPath.HR_LEAD,
            reason=f"PII exposure combined with policy flags: {policy_flags}.",
            pii_findings=pii_findings,
            keyword_flags=high_kw,
            policy_flags=policy_flags,
            sanitised_text=sanitised,
        )

    if policy_flags and prior_complaints >= 2:
        return GuardrailResult(
            passed=False,
            risk_level=RiskLevel.HIGH,
            action_tier=ActionTier.NEEDS_APPROVAL,
            escalation_path=EscalationPath.HR_LEAD,
            reason=f"Pattern alert: {prior_complaints} prior complaints + new policy flags.",
            pii_findings=pii_findings,
            keyword_flags=high_kw,
            policy_flags=policy_flags,
            sanitised_text=sanitised,
        )

    # --- MEDIUM: PII or policy flag, single occurrence
    if pii_findings or policy_flags or high_kw:
        return GuardrailResult(
            passed=True,            # Passes but with a flag
            risk_level=RiskLevel.MEDIUM,
            action_tier=ActionTier.NEEDS_APPROVAL,
            escalation_path=EscalationPath.HRBP_REVIEW,
            reason=f"Medium-risk flags: PII={bool(pii_findings)}, policy={bool(policy_flags)}, high_kw={bool(high_kw)}.",
            pii_findings=pii_findings,
            keyword_flags=high_kw,
            policy_flags=policy_flags,
            sanitised_text=sanitised,
        )

    # --- LOW: All clear
    return GuardrailResult(
        passed=True,
        risk_level=RiskLevel.LOW,
        action_tier=ActionTier.AUTO_EXECUTE,
        escalation_path=EscalationPath.AUTO_LOG,
        reason="No PII, critical keywords, or policy flags detected.",
        sanitised_text=sanitised,
    )


# ============================================================
# ACTION CONSTRAINT CHECKER
# ============================================================

def check_action_constraint(
    action_type: str,
    agent_name: str,
    context: Optional[Dict] = None,
) -> ActionConstraintResult:
    """
    Determine whether an agent is allowed to execute an action autonomously.

    Args:
        action_type: The type of action (must match DEFAULT_ACTION_TIERS key).
        agent_name: The agent attempting the action.
        context: Optional override context (e.g. from orchestrator).

    Returns:
        ActionConstraintResult indicating whether action is allowed and at what tier.
    """
    context = context or {}
    tier = DEFAULT_ACTION_TIERS.get(action_type, ActionTier.NEEDS_APPROVAL)

    # Context overrides (orchestrator can escalate a tier)
    context_tier = context.get("force_tier")
    if context_tier:
        try:
            tier = ActionTier(context_tier)
        except ValueError:
            pass

    if tier == ActionTier.AUTO_EXECUTE:
        return ActionConstraintResult(
            allowed=True,
            action_tier=tier,
            reason=f"Action '{action_type}' is in auto-execute tier.",
        )
    elif tier == ActionTier.NEEDS_APPROVAL:
        approver = context.get("approver", "owner")
        return ActionConstraintResult(
            allowed=False,
            action_tier=tier,
            reason=f"Action '{action_type}' requires human approval before execution.",
            requires_approval_from=approver,
        )
    else:  # BLOCKED
        return ActionConstraintResult(
            allowed=False,
            action_tier=tier,
            reason=f"Action '{action_type}' is blocked. Agent may only propose this action, not execute it.",
        )


# ============================================================
# QUICK USAGE EXAMPLE
# ============================================================
if __name__ == "__main__":
    sample_text = """
    Hi, I wanted to flag that my manager has been excluding me from important meetings
    since I requested flexible working last month. My email is jane.doe@example.com
    and my salary of £45,000 hasn't been discussed in my recent appraisal either.
    """

    result = evaluate(sample_text, sender_history={"prior_complaints_count": 1})

    print(f"Risk Level:       {result.risk_level}")
    print(f"Action Tier:      {result.action_tier}")
    print(f"Escalation:       {result.escalation_path}")
    print(f"Reason:           {result.reason}")
    print(f"PII Found:        {result.pii_findings}")
    print(f"Policy Flags:     {result.policy_flags}")
    print(f"Sanitised Text:   {result.sanitised_text[:120]}...")
