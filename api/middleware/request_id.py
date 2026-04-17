"""
middleware/request_id.py — Request ID Injection Middleware
===========================================================
Injects a UUID into every request so all log lines for a single request
share a common `request_id` field. The ID is:

  1. Taken from the incoming `X-Request-ID` header if provided (allows
     callers like n8n to set their own trace IDs for correlation).
  2. Generated as a new UUID4 if no header is present.

The ID is:
  - Bound into structlog's context vars so every log call during the request
    automatically includes `request_id` without passing it around manually.
  - Echoed back in the `X-Request-ID` response header so clients can include
    it in bug reports.

Usage:
    from middleware.request_id import RequestIDMiddleware
    app.add_middleware(RequestIDMiddleware)
"""

import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that assigns a unique request ID to every HTTP request.
    Compatible with FastAPI / Starlette.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Honour caller-supplied ID; fall back to a fresh UUID
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())

        # Clear any stale context from a previous request on this thread/task
        structlog.contextvars.clear_contextvars()

        # Bind to structlog so all log calls during this request see it
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        # Process the request
        response: Response = await call_next(request)

        # Echo the ID back to the caller
        response.headers["x-request-id"] = request_id

        return response
