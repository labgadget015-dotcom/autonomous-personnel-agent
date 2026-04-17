"""
logging_config.py — Structured JSON Logging
=============================================
Configures structlog to emit every log line as a JSON object with
consistent fields. This makes logs filterable and searchable in
Datadog, CloudWatch, Loki, or any JSON-capable log aggregator.

Every log line produced after configure_logging() is called will look like:
    {
      "event": "request_complete",
      "request_id": "f47ac10b-58cc-4372-a567",
      "method": "POST",
      "path": "/talent/screen",
      "status_code": 200,
      "latency_ms": 843,
      "level": "info",
      "timestamp": "2026-04-17T07:00:00.000000Z",
      "logger": "personnel-agent"
    }

Usage:
    from logging_config import configure_logging, get_logger

    configure_logging()            # call once at startup in main.py
    log = get_logger("my.module")
    log.info("something happened", key="value")
"""

import logging
import os
import sys
from typing import Any

import structlog


def configure_logging(level: str | None = None) -> None:
    """
    Set up structlog with JSON output.

    Args:
        level: Log level string ("DEBUG", "INFO", "WARNING", "ERROR").
               Defaults to LOG_LEVEL env var, then "INFO".
    """
    log_level_str = level or os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # ---- stdlib root logger: direct all stdlib loggers through structlog ----
    logging.basicConfig(
        format="%(message)s",          # structlog handles actual formatting
        stream=sys.stdout,
        level=log_level,
        force=True,                    # override any existing basicConfig
    )

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("langchain").setLevel(logging.WARNING)

    # ---- structlog chain ----
    shared_processors: list[Any] = [
        # Merge context vars (request_id etc.) injected by RequestIDMiddleware
        structlog.contextvars.merge_contextvars,
        # Add log level as a field
        structlog.stdlib.add_log_level,
        # Add logger name
        structlog.stdlib.add_logger_name,
        # ISO-8601 timestamp
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Render exception tracebacks inline
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    structlog.configure(
        processors=shared_processors + [structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "personnel-agent") -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to `name`."""
    return structlog.get_logger(name)
