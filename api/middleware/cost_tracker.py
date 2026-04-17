"""
middleware/cost_tracker.py — LLM Token Cost Tracking
======================================================
A LangChain callback handler that logs token usage for every LLM call.
This gives per-agent cost visibility without any external APM service.

What it does:
  - Logs prompt_tokens, completion_tokens, total_tokens, and model name
    after every LLM completion
  - Estimates USD cost using a built-in pricing table (updated for 2026)
  - Accumulates totals in a thread-safe counter (visible at /metrics)
  - All logs include request_id from structlog context vars so you can
    correlate cost to specific API requests

Usage:
    from middleware.cost_tracker import CostTrackingCallback, get_cumulative_cost

    # Pass to any LangChain LLM:
    llm = ChatOpenAI(
        model="gpt-4.1",
        callbacks=[CostTrackingCallback(agent="talent")],
    )

    # Get cumulative totals (for /metrics endpoint):
    cost_data = get_cumulative_cost()
"""

import threading
from typing import Any, Dict, Optional

import structlog

log = structlog.get_logger("personnel-agent.cost")

# ---- Pricing table (USD per 1k tokens, as of April 2026) ----
# Update this dict when OpenAI changes pricing.
_PRICING: Dict[str, Dict[str, float]] = {
    "gpt-4.1":          {"input": 0.002,  "output": 0.008},
    "gpt-4.1-mini":     {"input": 0.0004, "output": 0.0016},
    "gpt-4.1-nano":     {"input": 0.0001, "output": 0.0004},
    "gpt-4o":           {"input": 0.0025, "output": 0.010},
    "gpt-4o-mini":      {"input": 0.00015,"output": 0.0006},
    "gpt-4-turbo":      {"input": 0.01,   "output": 0.03},
    "gpt-3.5-turbo":    {"input": 0.0005, "output": 0.0015},
}
_DEFAULT_PRICING = {"input": 0.002, "output": 0.008}  # fallback for unknown models

# ---- Thread-safe cumulative counters ----
_lock = threading.Lock()
_cumulative: Dict[str, Any] = {
    "total_prompt_tokens": 0,
    "total_completion_tokens": 0,
    "total_tokens": 0,
    "estimated_usd": 0.0,
    "calls": 0,
    "by_agent": {},
    "by_model": {},
}


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return estimated USD cost for a single LLM call."""
    pricing = _PRICING.get(model, _DEFAULT_PRICING)
    return (
        (prompt_tokens * pricing["input"] / 1000) +
        (completion_tokens * pricing["output"] / 1000)
    )


def get_cumulative_cost() -> Dict[str, Any]:
    """Return a snapshot of cumulative token usage and cost."""
    with _lock:
        return dict(_cumulative)


class CostTrackingCallback:
    """
    LangChain callback that logs and accumulates token usage.

    Compatible with langchain>=0.3 (uses BaseCallbackHandler interface
    without subclassing to avoid import issues if langchain is mocked in tests).
    """

    def __init__(self, agent: str = "unknown", request_id: Optional[str] = None):
        self.agent = agent
        self.request_id = request_id

    # ---- LangChain BaseCallbackHandler interface ----

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """Called after every LLM completion."""
        try:
            llm_output = getattr(response, "llm_output", {}) or {}
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
            model = llm_output.get("model_name", "unknown")

            prompt_tokens     = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            total_tokens      = int(usage.get("total_tokens", prompt_tokens + completion_tokens))
            estimated_usd     = _estimate_cost(model, prompt_tokens, completion_tokens)

            # Structured log (includes request_id from structlog context vars)
            log.info(
                "llm_cost",
                agent=self.agent,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_usd=round(estimated_usd, 6),
            )

            # Update cumulative counters
            with _lock:
                _cumulative["total_prompt_tokens"]     += prompt_tokens
                _cumulative["total_completion_tokens"] += completion_tokens
                _cumulative["total_tokens"]            += total_tokens
                _cumulative["estimated_usd"]           = round(_cumulative["estimated_usd"] + estimated_usd, 6)
                _cumulative["calls"]                   += 1

                # Per-agent breakdown
                if self.agent not in _cumulative["by_agent"]:
                    _cumulative["by_agent"][self.agent] = {
                        "tokens": 0, "estimated_usd": 0.0, "calls": 0
                    }
                _cumulative["by_agent"][self.agent]["tokens"]        += total_tokens
                _cumulative["by_agent"][self.agent]["estimated_usd"] = round(
                    _cumulative["by_agent"][self.agent]["estimated_usd"] + estimated_usd, 6
                )
                _cumulative["by_agent"][self.agent]["calls"] += 1

                # Per-model breakdown
                if model not in _cumulative["by_model"]:
                    _cumulative["by_model"][model] = {
                        "tokens": 0, "estimated_usd": 0.0, "calls": 0
                    }
                _cumulative["by_model"][model]["tokens"]        += total_tokens
                _cumulative["by_model"][model]["estimated_usd"] = round(
                    _cumulative["by_model"][model]["estimated_usd"] + estimated_usd, 6
                )
                _cumulative["by_model"][model]["calls"] += 1

        except Exception as exc:
            # Never let cost tracking crash the agent call
            log.warning("cost_tracker_error", error=str(exc))

    def on_llm_error(self, error: Exception, **kwargs: Any) -> None:
        log.warning("llm_error", agent=self.agent, error=str(error))

    def on_chain_start(self, serialized: Any, inputs: Any, **kwargs: Any) -> None:
        pass

    def on_chain_end(self, outputs: Any, **kwargs: Any) -> None:
        pass

    def on_chain_error(self, error: Exception, **kwargs: Any) -> None:
        log.warning("chain_error", agent=self.agent, error=str(error))
