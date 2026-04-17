"""
middleware/rate_limit.py — Per-Token Rate Limiting
====================================================
Uses slowapi (a Starlette/FastAPI wrapper for limits) backed by Redis
to enforce per-API-token rate limits. This prevents:

  - Cost overrun from a leaked x-api-token
  - LLM tier flooding from a runaway integration
  - Abuse of the candidate screening or knowledge endpoints

Default limits (configurable via env vars):
  - RATE_LIMIT_DEFAULT    : 60/minute  (general endpoints)
  - RATE_LIMIT_AGENT      : 20/minute  (agent endpoints — LLM calls)
  - RATE_LIMIT_HEALTH     : 120/minute (health probes — no LLM)

Key function: per API token (from x-api-token header), not per IP.
This is correct for a service where all callers share one egress IP
(e.g. n8n running behind a NAT gateway).

Usage in main.py:
    from middleware.rate_limit import limiter, rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

Usage on a route:
    from middleware.rate_limit import limiter

    @app.post("/talent/screen")
    @limiter.limit(AGENT_RATE_LIMIT)
    async def screen_candidate(request: Request, ...):
        ...

Note: `request: Request` must be a parameter on any rate-limited route.
"""

import os
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

# ---- Rate limit strings (configurable via env) ----
DEFAULT_RATE_LIMIT = os.getenv("RATE_LIMIT_DEFAULT", "60/minute")
AGENT_RATE_LIMIT   = os.getenv("RATE_LIMIT_AGENT",   "20/minute")
HEALTH_RATE_LIMIT  = os.getenv("RATE_LIMIT_HEALTH",  "120/minute")


def _key_from_token(request: Request) -> str:
    """
    Rate-limit key: use the API token so each caller gets their own bucket.
    Falls back to client IP if the token header is absent (e.g. /health).
    """
    token = request.headers.get("x-api-token")
    if token:
        # Hash the token so it's not logged in Redis keys
        import hashlib
        return "tok:" + hashlib.sha256(token.encode()).hexdigest()[:16]
    # Fallback: client IP (handles /health, /docs)
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return "ip:" + forwarded_for.split(",")[0].strip()
    return "ip:" + (request.client.host if request.client else "unknown")


# ---- Limiter instance ----
_redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

limiter = Limiter(
    key_func=_key_from_token,
    default_limits=[DEFAULT_RATE_LIMIT],
    storage_uri=_redis_url,
    # Fail open: if Redis is unavailable, allow the request rather than blocking
    # (prevents Redis outage from taking down the API)
    enabled=True,
    swallow_errors=True,
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """Return a clear 429 JSON response when a rate limit is hit."""
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": f"Too many requests. Limit: {exc.detail}. "
                      "Check the Retry-After header and slow down.",
            "retry_after": getattr(exc, "retry_after", None),
        },
        headers={"Retry-After": str(getattr(exc, "retry_after", 60))},
    )
