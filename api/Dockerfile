# ============================================================
# Autonomous Personnel Agent — FastAPI Service
# Multi-stage build for minimal production image
# ============================================================

# Stage 1: builder — install dependencies into a venv
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies (needed for psycopg2, faiss-cpu)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment
RUN python -m venv /build/venv
ENV PATH="/build/venv/bin:$PATH"

# Install Python dependencies first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ============================================================
# Stage 2: production image — lean, no build tools
# ============================================================
FROM python:3.11-slim AS production

# Build-time metadata injected by CI (docker/build-push-action build-args)
# These become environment variables readable by health.py at runtime
ARG BUILD_VERSION=dev
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown

ENV BUILD_VERSION=${BUILD_VERSION} \
    BUILD_SHA=${BUILD_SHA} \
    BUILD_DATE=${BUILD_DATE}

LABEL org.opencontainers.image.title="Personnel Agent API"
LABEL org.opencontainers.image.description="Autonomous multi-agent personnel management — FastAPI service"
LABEL org.opencontainers.image.source="https://github.com/labgadget015-dotcom/autonomous-personnel-agent"
LABEL org.opencontainers.image.version=${BUILD_VERSION}
LABEL org.opencontainers.image.revision=${BUILD_SHA}

WORKDIR /app

# Runtime system dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder stage
COPY --from=builder /build/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# Create non-root user for security
RUN groupadd --gid 1001 appuser && \
    useradd --uid 1001 --gid 1001 --no-create-home --shell /bin/false appuser

# Copy application code
COPY --chown=appuser:appuser . .

# Remove .env.example from production image
RUN rm -f .env.example

# Switch to non-root user
USER appuser

# Expose API port
EXPOSE 8000

# Liveness probe — uses /health/live (no external I/O, always fast)
# Deep health is available at /health for monitoring dashboards
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8000/health/live || exit 1

# Default command — 2 workers for local; override in production
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-level", "info", \
     "--access-log"]
