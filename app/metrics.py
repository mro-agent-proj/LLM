"""Prometheus metrics for the AI server (scraped at /metrics).

Labels carry only outcomes, reason codes, schema and tool names, never request text or model output.
"""

from prometheus_client import Counter, Gauge, Histogram

RUNS = Counter(
    "mro_ai_runs_total",
    "Agent runs that reached a terminal event, by outcome and reason",
    ["outcome", "reason"],
)
RUN_SECONDS = Histogram(
    "mro_ai_run_duration_seconds",
    "Wall time of one run attempt until its terminal event",
    ["outcome"],
    buckets=(2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 280),
)
RUNS_IN_PROGRESS = Gauge(
    "mro_ai_runs_in_progress", "Runs being executed now (single consumer: 0 or 1)"
)
LLM_CALLS = Counter(
    "mro_ai_llm_calls_total",
    "Structured model calls by output schema and result (ok, validation_error, timeout, error)",
    ["schema", "result"],
)
LLM_SECONDS = Histogram(
    "mro_ai_llm_call_duration_seconds",
    "Latency of one structured model call",
    ["schema"],
    buckets=(0.5, 1, 2, 3, 5, 8, 12, 20, 30, 45),
)
TOOL_CALLS = Counter(
    "mro_ai_tool_calls_total",
    "MCP tool calls made by the agent, by outcome (OK, EMPTY, NEEDS_INPUT, ERROR)",
    ["tool", "outcome"],
)
TOOL_SECONDS = Histogram(
    "mro_ai_tool_call_duration_seconds",
    "MCP tool call latency seen by the agent, retries included",
    ["tool"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20),
)


def record_run(payload: dict, seconds: float) -> None:
    if "error" in payload and "outcome" not in payload:
        outcome, reason = "FAILED", str(payload["error"])
    else:
        outcome = str(payload.get("outcome", "UNKNOWN"))
        reason = str(payload.get("review_reason") or ("NONE" if outcome == "ASSIGN" else "UNKNOWN"))
    RUNS.labels(outcome, reason).inc()
    RUN_SECONDS.labels(outcome).observe(seconds)
