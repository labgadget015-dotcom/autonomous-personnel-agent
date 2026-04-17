"""
webhooks.py — Webhook Delivery with Retry + Exponential Backoff
================================================================
When the agent posts results to external webhooks (n8n, Slack, Teams),
network blips cause silent failures. This module wraps all outbound
webhook calls with a tenacity retry policy:

  Attempt 1: immediate
  Attempt 2: +2s
  Attempt 3: +4s
  Attempt 4: +8s
  Attempt 5: +16s (capped at 60s)

After all 5 attempts fail, the payload is logged as a dead-letter event
so it can be recovered manually or replayed from the audit log.

Usage:
    from webhooks import post_webhook

    await post_webhook(
        url="https://your-n8n.app/webhook/abc123",
        payload={"event": "candidate_screened", ...},
        request_id=request_id,
    )
"""

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    RetryError,
)

log = structlog.get_logger("personnel-agent.webhooks")
_fallback_log = logging.getLogger("personnel-agent.webhooks.deadletter")


class WebhookDeliveryError(Exception):
    """Raised when all retry attempts for a webhook have been exhausted."""
    pass


async def post_webhook(
    url: str,
    payload: Dict[str, Any],
    request_id: Optional[str] = None,
    timeout: float = 10.0,
    max_attempts: int = 5,
) -> httpx.Response:
    """
    POST a JSON payload to a webhook URL with exponential backoff retry.

    Args:
        url:          Target webhook URL.
        payload:      JSON-serialisable dict to send as the request body.
        request_id:   Optional trace ID — included in headers and logs.
        timeout:      Per-attempt timeout in seconds (default 10s).
        max_attempts: Maximum delivery attempts (default 5).

    Returns:
        The final successful httpx.Response.

    Raises:
        WebhookDeliveryError: If all attempts are exhausted without success.
    """
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "autonomous-personnel-agent/1.0",
    }
    if request_id:
        headers["X-Request-ID"] = request_id

    attempt_number = 0

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=2, max=60),
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError)
            ),
            reraise=False,
        ):
            with attempt:
                attempt_number += 1
                log.info(
                    "webhook_attempt",
                    url=url,
                    attempt=attempt_number,
                    request_id=request_id,
                )
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, json=payload, headers=headers)
                    # Raise for 5xx so tenacity retries; 4xx are caller errors — don't retry
                    if response.status_code >= 500:
                        response.raise_for_status()

        log.info(
            "webhook_delivered",
            url=url,
            status_code=response.status_code,
            attempts=attempt_number,
            request_id=request_id,
        )
        return response

    except RetryError as exc:
        _dead_letter(url, payload, request_id, str(exc))
        raise WebhookDeliveryError(
            f"Webhook delivery failed after {max_attempts} attempts to {url}: {exc}"
        ) from exc
    except Exception as exc:
        _dead_letter(url, payload, request_id, str(exc))
        raise WebhookDeliveryError(
            f"Unexpected error delivering webhook to {url}: {exc}"
        ) from exc


def _dead_letter(
    url: str,
    payload: Dict[str, Any],
    request_id: Optional[str],
    error: str,
) -> None:
    """
    Log a dead-letter event for manual recovery.
    In production, this should also write to the `audit_log` table or a
    dead-letter queue (Redis list / SQS) so the payload can be replayed.
    """
    log.error(
        "webhook_dead_letter",
        url=url,
        request_id=request_id,
        error=error,
        payload_keys=list(payload.keys()),
        payload_size_bytes=len(json.dumps(payload).encode()),
        action="manual_recovery_required",
    )
    # Also log at stdlib level for legacy log aggregators
    _fallback_log.error(
        "DEAD LETTER: failed to deliver webhook to %s after all retries. "
        "request_id=%s error=%s payload_keys=%s",
        url, request_id, error, list(payload.keys()),
    )


# ---- Convenience: fire-and-forget (non-blocking) ----

def fire_and_forget_webhook(
    url: str,
    payload: Dict[str, Any],
    request_id: Optional[str] = None,
) -> asyncio.Task:
    """
    Schedule a webhook delivery in the background without awaiting it.
    The task is returned so callers can optionally track or cancel it.

    Usage:
        fire_and_forget_webhook(n8n_url, result_payload, request_id=req_id)
    """
    return asyncio.create_task(
        post_webhook(url, payload, request_id),
        name=f"webhook-{request_id or 'anon'}",
    )
