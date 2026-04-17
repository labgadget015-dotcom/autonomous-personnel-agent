"""
telemetry.py — OpenTelemetry Distributed Tracing
==================================================
Instruments the entire Personnel Agent stack so that a single stakeholder
request produces one continuous trace:

  HTTP request (FastAPI span)
    └── arq enqueue  (httpx span to Redis)
          └── arq worker job (arq span)
                └── LangChain chain  (langchain span)
                      └── OpenAI completion (openai span)
                            └── Postgres query (psycopg2 span)

Every span carries:
  - trace_id  — propagated from the HTTP X-Request-ID header
  - request_id, method, path, agent, job_id as span attributes
  - structlog context vars (merged by tracing middleware)

Exporter:
  - OTLP gRPC to the otel-collector service (default: otel-collector:4317)
  - Fallback: console exporter if OTEL_EXPORTER_OTLP_ENDPOINT is not set
  - Disabled entirely if OTEL_ENABLED=false

Usage in main.py:
    from telemetry import configure_tracing
    configure_tracing(app)          # call once, after app = FastAPI(...)

Usage for custom spans in worker / agents:
    from telemetry import get_tracer
    tracer = get_tracer("personnel-agent.worker")
    with tracer.start_as_current_span("run_talent_screen") as span:
        span.set_attribute("agent", "talent")
        span.set_attribute("job_id", job_id)
        result = await do_work()
"""

import logging
import os
from typing import Optional

logger = logging.getLogger("personnel-agent.telemetry")

# ---- Feature flag ----
_OTEL_ENABLED = os.getenv("OTEL_ENABLED", "true").lower() not in ("false", "0", "no")
_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "autonomous-personnel-agent")
_SERVICE_VERSION = os.getenv("BUILD_VERSION", "dev")
_ENVIRONMENT = os.getenv("ENV", "production")

# Will be set to the real TracerProvider on configure_tracing()
_tracer_provider = None


def configure_tracing(app=None) -> None:
    """
    Set up OpenTelemetry SDK and instrument FastAPI, httpx, psycopg2.

    Args:
        app: FastAPI application instance (required for FastAPI instrumentation).

    Silently no-ops if OTEL_ENABLED=false or required packages are missing.
    """
    global _tracer_provider

    if not _OTEL_ENABLED:
        logger.info("OpenTelemetry disabled (OTEL_ENABLED=false)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
        from opentelemetry.semconv.resource import ResourceAttributes

        # ---- Resource ----
        resource = Resource.create({
            SERVICE_NAME: _SERVICE_NAME,
            SERVICE_VERSION: _SERVICE_VERSION,
            ResourceAttributes.DEPLOYMENT_ENVIRONMENT: _ENVIRONMENT,
        })

        provider = TracerProvider(resource=resource)

        # ---- Exporter ----
        if _OTLP_ENDPOINT:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                exporter = OTLPSpanExporter(endpoint=_OTLP_ENDPOINT, insecure=True)
                provider.add_span_processor(BatchSpanProcessor(exporter))
                logger.info("OTel OTLP exporter configured → %s", _OTLP_ENDPOINT)
            except ImportError:
                logger.warning("opentelemetry-exporter-otlp-proto-grpc not installed; "
                               "falling back to console exporter")
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        else:
            # No collector configured — use console exporter (visible in docker logs)
            logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — using console span exporter")
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        _tracer_provider = provider

        # ---- Instrument FastAPI ----
        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
                FastAPIInstrumentor.instrument_app(
                    app,
                    tracer_provider=provider,
                    excluded_urls="/health/live,/docs,/redoc,/openapi.json",
                )
                logger.info("FastAPI instrumented with OTel")
            except ImportError:
                logger.warning("opentelemetry-instrumentation-fastapi not installed")

        # ---- Instrument httpx ----
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            HTTPXClientInstrumentor().instrument(tracer_provider=provider)
            logger.info("httpx instrumented with OTel")
        except ImportError:
            logger.warning("opentelemetry-instrumentation-httpx not installed")

        # ---- Instrument SQLAlchemy ----
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument(tracer_provider=provider)
            logger.info("SQLAlchemy instrumented with OTel")
        except ImportError:
            logger.warning("opentelemetry-instrumentation-sqlalchemy not installed")

        # ---- Instrument Redis (arq uses redis-py) ----
        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor
            RedisInstrumentor().instrument(tracer_provider=provider)
            logger.info("Redis instrumented with OTel")
        except ImportError:
            logger.warning("opentelemetry-instrumentation-redis not installed")

        logger.info("OpenTelemetry tracing configured (service=%s, version=%s)",
                    _SERVICE_NAME, _SERVICE_VERSION)

    except ImportError as exc:
        logger.warning("OpenTelemetry SDK not installed — tracing disabled. "
                       "Install opentelemetry-sdk to enable. Error: %s", exc)
    except Exception as exc:
        logger.error("Failed to configure OpenTelemetry tracing: %s", exc, exc_info=True)


def get_tracer(name: str = "personnel-agent"):
    """
    Return a tracer for the given instrumentation scope.
    Returns a no-op tracer if OTel is not configured.
    """
    try:
        from opentelemetry import trace
        return trace.get_tracer(name, _SERVICE_VERSION)
    except ImportError:
        return _NoOpTracer()


def current_trace_id() -> Optional[str]:
    """Return the current trace ID as a hex string, or None if no active span."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.trace_id, "032x")
        return None
    except (ImportError, Exception):
        return None


def current_span_id() -> Optional[str]:
    """Return the current span ID as a hex string, or None if no active span."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            return format(ctx.span_id, "016x")
        return None
    except (ImportError, Exception):
        return None


def span_from_request_id(request_id: str):
    """
    Create a context manager that starts a new span linked to the given
    request_id. Use this in the arq worker to connect job spans back to
    the originating HTTP request.
    """
    tracer = get_tracer("personnel-agent.worker")
    try:
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        # Use the request_id as the trace ID (first 32 hex chars)
        # This creates a logical link even when W3C propagation headers are absent
        trace_id_int = int(request_id.replace("-", "")[:32].ljust(32, "0"), 16)
        parent_ctx = SpanContext(
            trace_id=trace_id_int,
            span_id=0,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        return tracer.start_as_current_span(
            "arq_job",
            context=_context_with_span(NonRecordingSpan(parent_ctx)),
        )
    except Exception:
        return tracer.start_as_current_span("arq_job")


def _context_with_span(span):
    """Wrap a span into an OpenTelemetry Context."""
    try:
        from opentelemetry import context, trace
        return trace.set_span_in_context(span)
    except Exception:
        return None


# ---- No-op fallback ----

class _NoOpSpan:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def set_attribute(self, *args): pass
    def set_status(self, *args): pass
    def record_exception(self, *args): pass


class _NoOpTracer:
    def start_as_current_span(self, name, **kwargs):
        return _NoOpSpan()
    def start_span(self, name, **kwargs):
        return _NoOpSpan()
