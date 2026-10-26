"""Prometheus scrape: one scripted run is counted by outcome, model schema and tool."""

import pytest
from fastapi.testclient import TestClient

from app.agent.runtime import AgentRuntime
from app.agent.workflow import build_graph, completion_payload
from app.main import create_app
from app.metrics import record_run
from tests.support import DemoTools, ScriptedDemoModel, example_state


def test_metrics_endpoint_exposes_agent_metrics():
    record_run({"outcome": "HUMAN_REVIEW", "review_reason": "AGENT_NEEDS_HUMAN"}, 12.0)
    record_run({"error": "AI_EXECUTION_FAILED"}, 3.0)
    with TestClient(create_app()) as client:
        text = client.get("/metrics").text
    assert 'mro_ai_runs_total{outcome="HUMAN_REVIEW",reason="AGENT_NEEDS_HUMAN"}' in text
    assert 'mro_ai_runs_total{outcome="FAILED",reason="AI_EXECUTION_FAILED"}' in text
    assert "mro_ai_runs_in_progress" in text


@pytest.mark.asyncio
async def test_tool_calls_are_counted_by_tool_and_outcome():
    from app.metrics import TOOL_CALLS

    before = TOOL_CALLS.labels("search_sdr_cases", "OK")._value.get()
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=DemoTools())
    state = await build_graph(runtime).ainvoke(example_state())
    assert (await completion_payload(state, runtime))["outcome"] == "ASSIGN"
    assert TOOL_CALLS.labels("search_sdr_cases", "OK")._value.get() > before


@pytest.mark.asyncio
async def test_llm_calls_are_counted_by_schema_and_result():
    from app.agent.model import LangChainModel
    from app.agent.schemas import NextStep
    from app.metrics import LLM_CALLS

    # the first output breaks the schema (CALL without tool), the repair round succeeds
    fields = {"tool_name": None, "arguments_json": None, "hold_reason": None}
    outputs = iter(
        [
            {**fields, "action": "CALL", "question": "q"},
            {**fields, "action": "FINISH", "question": "q"},
        ]
    )

    class Runnable:
        async def ainvoke(self, messages):
            return next(outputs)

    class Model:
        def with_structured_output(self, schema):
            return Runnable()

    ok, bad = (LLM_CALLS.labels("NextStep", r)._value.get() for r in ("ok", "validation_error"))
    step = await LangChainModel(Model()).complete(NextStep, "system", "context")
    assert step.action == "FINISH"
    assert LLM_CALLS.labels("NextStep", "validation_error")._value.get() == bad + 1
    assert LLM_CALLS.labels("NextStep", "ok")._value.get() == ok + 1
