"""
health.py — Deep health check for the Personnel Agent API
==========================================================

Checks:
  1. Service liveness — always responds if the process is running
  2. PostgreSQL connectivity — connects, runs SELECT 1, measures latency
  3. Agent readiness — verifies LLM client can be instantiated (no token spent)
  4. Environment completeness — confirms required env vars are set
  5. Disk space — warns if knowledge vectorstore volume is near capacity

Response schema:
  {
    "status": "ok" | "degraded" | "unhealthy",
    "service": "autonomous-personnel-agent",
    "version": "1.0.0",
    "build": { "version": "...", "sha": "...", "date": "..." },
    "timestamp": "ISO-8601",
    "uptime_seconds": 12345,
    "checks": {
      "postgres":    { "status": "ok|fail", "latency_ms": 4, "detail": "..." },
      "llm_client":  { "status": "ok|fail", "model": "gpt-4.1", "detail": "..." },
      "environment": { "status": "ok|warn", "missing": [], "detail": "..." },
      "disk":        { "status": "ok|warn", "free_gb": 12.4, "detail": "..." }
    }
  }

HTTP status codes:
  200 — status is "ok" or "degraded" (degraded = some non-critical checks failed)
  503 — status is "unhealthy" (Postgres unreachable = cannot serve requests)
"""

import os
import time
import shutil
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Tuple

logger = logging.getLogger("personnel-agent.health")

# Build metadata injected at image build time
BUILD_VERSION = os.getenv("BUILD_VERSION", "dev")
BUILD_SHA     = os.getenv("BUILD_SHA", "unknown")
BUILD_DATE    = os.getenv("BUILD_DATE", "unknown")

# Service start time (module-level — survives request lifecycle)
_SERVICE_START = time.monotonic()
_SERVICE_START_WALL = datetime.now(timezone.utc).isoformat()

# Required environment variables for the service to operate
REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "API_TOKEN",
]

# Optional but warn if absent
RECOMMENDED_ENV_VARS = [
    "DATABASE_URL",
    "ORCHESTRATOR_MODEL",
    "AGENT_MODEL",
    "CORS_ORIGINS",
]


def _check_postgres() -> Dict[str, Any]:
    """
    Connect to Postgres, run SELECT 1, measure round-trip latency.
    Returns a check dict with status, latency_ms, and detail.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return {
            "status": "warn",
            "latency_ms": None,
            "detail": "DATABASE_URL not set — Postgres check skipped",
        }

    try:
        import psycopg2  # type: ignore[import]

        t0 = time.monotonic()
        conn = psycopg2.connect(database_url, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
                # Also get Postgres server version for extra detail
                cur.execute("SELECT version()")
                pg_version_row = cur.fetchone()
                pg_version = pg_version_row[0].split(",")[0] if pg_version_row else "unknown"
        finally:
            conn.close()

        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        return {
            "status": "ok",
            "latency_ms": latency_ms,
            "detail": pg_version,
        }

    except ImportError:
        return {
            "status": "warn",
            "latency_ms": None,
            "detail": "psycopg2 not installed — install psycopg2-binary to enable DB checks",
        }
    except Exception as exc:
        logger.warning("Postgres health check failed: %s", exc)
        return {
            "status": "fail",
            "latency_ms": None,
            "detail": str(exc),
        }


def _check_llm_client() -> Dict[str, Any]:
    """
    Verify the OpenAI client can be instantiated with the configured key.
    Does NOT make any API call or spend tokens — just validates config.
    """
    api_key = os.getenv("OPENAI_API_KEY", "")
    orchestrator_model = os.getenv("ORCHESTRATOR_MODEL", "gpt-4.1")
    agent_model = os.getenv("AGENT_MODEL", "gpt-4.1-mini")

    if not api_key:
        return {
            "status": "fail",
            "model": orchestrator_model,
            "detail": "OPENAI_API_KEY is not set",
        }

    if not api_key.startswith("sk-"):
        return {
            "status": "warn",
            "model": orchestrator_model,
            "detail": "OPENAI_API_KEY present but format looks unexpected (not sk-*). Key may be invalid.",
        }

    # Attempt client instantiation (no network call)
    try:
        from openai import OpenAI  # type: ignore[import]
        client = OpenAI(api_key=api_key)
        # Access the models attribute just to ensure the client is properly constructed
        _ = client.models
        return {
            "status": "ok",
            "orchestrator_model": orchestrator_model,
            "agent_model": agent_model,
            "detail": "Client instantiated successfully — key format valid",
        }
    except ImportError:
        return {
            "status": "warn",
            "model": orchestrator_model,
            "detail": "openai package not importable",
        }
    except Exception as exc:
        return {
            "status": "warn",
            "model": orchestrator_model,
            "detail": f"Client instantiation warning: {exc}",
        }


def _check_environment() -> Dict[str, Any]:
    """Check required and recommended environment variables."""
    missing_required = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    missing_recommended = [v for v in RECOMMENDED_ENV_VARS if not os.getenv(v)]

    if missing_required:
        return {
            "status": "fail",
            "missing_required": missing_required,
            "missing_recommended": missing_recommended,
            "detail": f"Critical env vars not set: {', '.join(missing_required)}",
        }

    if missing_recommended:
        return {
            "status": "warn",
            "missing_required": [],
            "missing_recommended": missing_recommended,
            "detail": f"Recommended env vars not set: {', '.join(missing_recommended)}",
        }

    return {
        "status": "ok",
        "missing_required": [],
        "missing_recommended": [],
        "detail": f"All {len(REQUIRED_ENV_VARS)} required vars present",
    }


def _check_disk() -> Dict[str, Any]:
    """Check available disk space on the knowledge vectorstore path."""
    knowledge_path = os.getenv("KNOWLEDGE_VECTORSTORE_PATH", "/app/knowledge_index")
    check_path = knowledge_path if os.path.exists(knowledge_path) else "/app"

    try:
        total, used, free = shutil.disk_usage(check_path)
        free_gb = round(free / (1024 ** 3), 2)
        used_pct = round((used / total) * 100, 1)

        if free_gb < 0.5:
            status = "fail"
            detail = f"CRITICAL: only {free_gb}GB free ({used_pct}% used) — service may fail"
        elif free_gb < 2.0:
            status = "warn"
            detail = f"Low disk: {free_gb}GB free ({used_pct}% used)"
        else:
            status = "ok"
            detail = f"{free_gb}GB free ({used_pct}% used)"

        return {
            "status": status,
            "free_gb": free_gb,
            "used_pct": used_pct,
            "path": check_path,
            "detail": detail,
        }
    except Exception as exc:
        return {
            "status": "warn",
            "free_gb": None,
            "detail": f"Could not check disk: {exc}",
        }


def run_health_checks(include_postgres: bool = True) -> Tuple[Dict[str, Any], int]:
    """
    Run all health checks and return (response_body, http_status_code).

    Args:
        include_postgres: Set False in unit tests to skip DB connection.

    Returns:
        Tuple of the full health response dict and the HTTP status code (200 or 503).
    """
    uptime_seconds = round(time.monotonic() - _SERVICE_START, 1)

    checks: Dict[str, Any] = {}

    # Run all checks
    checks["environment"] = _check_environment()
    checks["llm_client"]  = _check_llm_client()
    checks["disk"]        = _check_disk()

    if include_postgres:
        checks["postgres"] = _check_postgres()
    else:
        checks["postgres"] = {"status": "skip", "detail": "Skipped (no DATABASE_URL)"}

    # Determine overall status
    # - "unhealthy": any critical check failed (postgres fail, required env missing)
    # - "degraded": non-critical check failed or warned (disk warn, llm warn)
    # - "ok": everything passing
    critical_checks = ["postgres", "environment"]
    critical_statuses = {k: checks[k]["status"] for k in critical_checks if k in checks}
    all_statuses = [c["status"] for c in checks.values()]

    if any(s == "fail" for k, s in critical_statuses.items()):
        overall_status = "unhealthy"
        http_status = 503
    elif any(s in ("fail", "warn") for s in all_statuses):
        overall_status = "degraded"
        http_status = 200
    else:
        overall_status = "ok"
        http_status = 200

    response = {
        "status": overall_status,
        "service": "autonomous-personnel-agent",
        "version": "1.0.0",
        "build": {
            "version": BUILD_VERSION,
            "sha": BUILD_SHA,
            "date": BUILD_DATE,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "started_at": _SERVICE_START_WALL,
        "uptime_seconds": uptime_seconds,
        "checks": checks,
    }

    return response, http_status
