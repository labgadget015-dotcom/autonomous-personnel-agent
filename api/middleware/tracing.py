"""
middleware/tracing.py — OTel Trace Context → structlog + Response Headers
==========================================================================
Bridges OpenTelemetry trace context into structlog context vars so that
every log line emitted during a request carries both `request_id` AND the
OTel `trace_id` + `span_id`. This means:

  - Logs (Loki / CloudWatch) and traces (Tempo / Jaeger) share a common ID
  - Clicking a log line → jump to the full trace in Grafana
  - Clicking a trace span → see the exact log lines for that span

Also echoes the trace_id in the X-Trace-ID response header so n8n and
other callers can correlate their own logs to the OTel backend.

Must be registered AFTER RequestIDMiddleware (which sets request_id first).

Usage in main.py:
    from middleware.tracing import TracingContextMiddleware
    app.add_middleware(TracingContextMiddleware)
"""

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class TracingContextMiddleware(BaseHTTPMiddleware):
    """
    Reads the active OTel span (set by FastAPIInstrumentor) and binds
    trace_id + span_id into structlog context vars.

    Works even when OTel is not configured — silently skips if no span is
    active or if the opentelemetry package is not installed.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Add trace context to structlog (best-effort; never crashes the request)
        trace_id = None
        span_id = None

        try:
            from opentelemetry import trace as otel_trace
            span = otel_trace.get_current_span()
            ctx = span.get_span_context()
            if ctx and ctx.is_valid:
                trace_id = format(ctx.trace_id, "032x")
                span_id  = format(ctx.span_id,  "016x")
                structlog.contextvars.bind_contextvars(
                    trace_id=trace_id,
                    span_id=span_id,
                )
        except (ImportError, Exception):
            pass

        response: Response = await call_next(request)

        # Echo trace_id in response header for client correlation
        if trace_id:
            response.headers["x-trace-id"] = trace_id

        return response
