"""Exercise actual LangGraph input-channel filtering, not only prompt formatting."""

import copy
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import app.agent.full_workflow as full
from app.agent.nodes.isolation import merge_for_review, run_isolated_investigation
from app.agent.runtime import AgentRuntime
from app.agent.schemas import AgentContext, InvestigatorState, ReviewAssessment
from tests.llm1_fixtures import BusinessTools, FullModel
from tests.support import example_state


@pytest.mark.parametrize("resume", [False, True])
async def test_graph_never_routes_llm1_material_to_llm2_even_on_retry_and_resume(
    monkeypatch, resume
):
    inputs, reviewer_inputs = [], []
    original_review = full.run_review

    async def capture(state, runtime):
        inputs.append(copy.deepcopy(state))
        return await run_isolated_investigation(state, runtime)

    async def review(state, runtime):
        reviewer_inputs.append(copy.deepcopy(state))
        return await original_review(state, runtime)

    class Model(FullModel):
        async def complete(self, schema, system, context):
            result = await super().complete(schema, system, context)
            if schema is ReviewAssessment and result.review.decision == "SUPPLEMENT":
                result.review.supplement_request = "LLM3_PRIVATE_HISTORY MH-000123"
            return result

    monkeypatch.setattr(full, "run_isolated_investigation", capture)
    monkeypatch.setattr(full, "run_review", review)
    model = Model(False, supplements=1, rationale="LLM1_PRIVATE_JUDGMENT")
    tools = BusinessTools()
    tools.history[0]["symptom"] += " LLM1_PRIVATE_HISTORY"
    runtime = AgentRuntime(model, tools)
    graph = full.build_full_graph(runtime, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "isolation"}}
    if resume:
        await graph.ainvoke(example_state(), config, interrupt_after=["classify"])
        result = await graph.ainvoke(None, config)
    else:
        result = await graph.ainvoke(example_state(), config)
    assert len(inputs) == 2 and len(reviewer_inputs) == 2
    for value in inputs:
        assert set(value) <= set(InvestigatorState.__annotations__)
        assert not {"agent_context", "classification", "reviews", "stage_events"} & value.keys()
        text = json.dumps(value, default=str)
        for forbidden in ("LLM1_PRIVATE", "LLM3_PRIVATE", "MH-000123", "get_aircraft_history"):
            assert forbidden not in text
        assert all(
            r["source"] != "HISTORY" for r in value.get("llm2_context", {}).get("records", [])
        )
    for schema, payload in model.calls:
        if schema == "InvestigationAssessment" or (
            schema == "NextStep" and payload["stage"] == "INVESTIGATING"
        ):
            text = json.dumps(payload, default=str)
            for forbidden in ("LLM1_PRIVATE", "LLM3_PRIVATE", "MH-000123", "get_aircraft_history"):
                assert forbidden not in text
    for value in reviewer_inputs:
        assert "LLM1_PRIVATE_HISTORY" in json.dumps(value, default=str)
        assert value["classification"].rationale == "LLM1_PRIVATE_JUDGMENT"
        assert any(r["source"] == "SDR" for r in value["agent_context"]["records"])
    assert result["agent_context"]["prepared_payload"]["outcome"] == "ASSIGN"
    assert all(r["source"] != "HISTORY" for r in result["llm2_context"]["records"])


async def test_legacy_history_checkpoint_is_refused_before_any_model_or_tool_call():
    tools, model = BusinessTools(), FullModel(False)
    state = {k: v for k, v in example_state().items() if k in InvestigatorState.__annotations__}
    state["llm2_context"] = AgentContext(
        request_id=state["request_id"], classification_comparisons=[{"history_id": "MH-000123"}]
    ).model_dump()
    with pytest.raises(ValueError, match="foreign agent"):
        await run_isolated_investigation(state, AgentRuntime(model, tools))
    assert not model.calls and not tools.calls


def test_review_join_refuses_different_context_versions():
    state = example_state()
    state["agent_context"] = AgentContext(
        request_id=state["request_id"], context_version=1
    ).model_dump()
    state["llm2_context"] = AgentContext(
        request_id=state["request_id"], context_version=2
    ).model_dump()
    update = merge_for_review(state)
    assert update["agent_context"]["hold"]["reason"] == "CONTEXT_CHANGED"
