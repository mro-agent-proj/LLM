import copy
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.demo import DemoTools, ScriptedDemoModel, example_state
from app.agent.nodes.investigate import run_investigate
from app.agent.nodes.review import run_review
from app.agent.runtime import AgentRuntime
from app.agent.schemas import AgentContext, Investigation, NextStep, Review
from app.agent.settings import Settings
from app.agent.tools import EvidenceValidationError
from app.agent.workflow import build_graph, completion_payload


@pytest.mark.asyncio
async def test_full_pass_requires_fresh_document_validation():
    tools = DemoTools()
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    initial = example_state()
    saved_initial = copy.deepcopy(initial)
    state = await build_graph(runtime).ainvoke(initial)
    assert initial == saved_initial
    assert not any(name == "get_document" for name, _ in tools.calls)
    payload = await completion_payload(state, runtime)
    assert payload["outcome"] == "ASSIGN"
    assert payload["department"] == "HYD"
    assert any(name == "get_document" for name, _ in tools.calls)
    assert "agent_context" not in payload
    assert all(trace["question"] for trace in state["agent_context"]["traces"])


@pytest.mark.asyncio
async def test_no_evidence_produces_internal_hold_and_human_without_fake_investigation():
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=DemoTools(empty=True))
    state = await build_graph(runtime).ainvoke(example_state())
    assert state["investigations"] == []
    assert state["reviews"][-1].decision == "HUMAN"
    assert state["agent_context"]["hold"]["reason"] == "INSUFFICIENT_EVIDENCE"
    assert (await completion_payload(state, runtime))["review_reason"] == "AGENT_NEEDS_HUMAN"


@pytest.mark.asyncio
async def test_supplement_reaches_llm2_and_results_accumulate():
    model = ScriptedDemoModel(supplements=1)
    runtime = AgentRuntime(model=model, tools=DemoTools())
    state = await build_graph(runtime).ainvoke(example_state())
    assert state["retry_count"] == 1
    assert len(state["investigations"]) == 2
    assert [review.decision for review in state["reviews"]] == ["SUPPLEMENT", "PASS"]
    assert any(
        payload.get("latest_supplement_request") == "이전 사례의 발생 단계를 다시 비교하라."
        for schema, payload in model.calls
        if schema == "NextStep"
    )


@pytest.mark.asyncio
async def test_reinvestigation_limit_is_two_returns_not_three_returns():
    runtime = AgentRuntime(model=ScriptedDemoModel(supplements=10), tools=DemoTools())
    state = await build_graph(runtime).ainvoke(example_state())
    assert len(state["investigations"]) == 3
    assert state["retry_count"] == 2
    assert (await completion_payload(state, runtime))["review_reason"] == "RETRY_LIMIT"


@pytest.mark.asyncio
async def test_removed_document_cannot_be_assigned_from_stale_evidence():
    tools = DemoTools()
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    tools.rows = []
    payload = await completion_payload(state, runtime)
    assert payload["outcome"] == "HUMAN_REVIEW"
    assert payload["review_reason"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_inactive_department_cannot_be_assigned_from_stale_directory():
    tools = DemoTools()
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    tools.departments[0]["active"] = False
    assert (await completion_payload(state, runtime))["review_reason"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_fabricated_evidence_goes_to_validation_failed_not_pass():
    class FabricatingModel(ScriptedDemoModel):
        async def complete(self, schema, system, context):
            output = await super().complete(schema, system, context)
            if schema is Investigation:
                output.evidence[0].ref_id = "SDR-INVENTED"
            return output

    runtime = AgentRuntime(model=FabricatingModel(), tools=DemoTools())
    state = await build_graph(runtime).ainvoke(example_state())
    assert state["investigations"] == []
    assert (await completion_payload(state, runtime))["review_reason"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_model_exception_is_not_swallowed_by_node():
    class BrokenModel:
        async def complete(self, schema, system, context):
            raise RuntimeError("synthetic provider failure")

    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        await run_investigate(example_state(), AgentRuntime(model=BrokenModel(), tools=DemoTools()))


@pytest.mark.asyncio
async def test_unknown_tool_loop_is_bounded():
    class LoopModel:
        async def complete(self, schema, system, context):
            return NextStep(
                action="CALL",
                question="합성 금지 도구 시험",
                tool_name="assign_request",
                arguments_json="{}",
                hold_reason=None,
            )

    runtime = AgentRuntime(
        model=LoopModel(), tools=DemoTools(), settings=Settings(max_tool_calls=3)
    )
    state = await build_graph(runtime).ainvoke(example_state())
    assert state["agent_context"]["hold"]["reason"] == "TOOL_LIMIT"


@pytest.mark.asyncio
async def test_checkpoint_uses_request_id_and_contains_private_hold():
    saver = InMemorySaver()
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=DemoTools(empty=True))
    graph = build_graph(runtime, checkpointer=saver)
    config = {"configurable": {"thread_id": "DEMO-REQUEST-001"}}
    await graph.ainvoke(example_state(), config)
    checkpoint = graph.get_state(config)
    assert checkpoint.values["agent_context"]["request_id"] == "DEMO-REQUEST-001"
    assert checkpoint.values["agent_context"]["hold"]["reason"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.asyncio
async def test_context_cannot_be_reused_for_another_request():
    state = example_state()
    state["agent_context"] = AgentContext(request_id="OTHER").model_dump()
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=DemoTools())
    with pytest.raises(EvidenceValidationError, match="another request"):
        await run_review(state, runtime)


@pytest.mark.asyncio
async def test_internal_hold_review_does_not_call_the_model():
    class MustNotCallModel:
        async def complete(self, schema, system, context):
            raise AssertionError("Abstention must not fabricate a model result")

    state = example_state()
    state["agent_context"] = AgentContext.model_validate(
        {
            "request_id": state["request_id"],
            "hold": {"reason": "INSUFFICIENT_INFORMATION", "summary": "현장 측정값 필요"},
        }
    ).model_dump()
    update = await run_review(state, AgentRuntime(model=MustNotCallModel(), tools=DemoTools()))
    assert update["reviews"][-1] == Review(
        decision="HUMAN",
        department=None,
        comments="현장 측정값 필요",
        supplement_request=None,
    )


@pytest.mark.asyncio
async def test_resume_after_failed_review_keeps_investigation_without_repeating_it():
    class FailOnce(ScriptedDemoModel):
        failed = False

        async def complete(self, schema, system, context):
            if schema is NextStep and json.loads(context).get("stage") == "REVIEWING":
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("synthetic review interruption")
            return await super().complete(schema, system, context)

    runtime = AgentRuntime(model=FailOnce(), tools=DemoTools())
    graph = build_graph(runtime, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "DEMO-REQUEST-001"}}
    with pytest.raises(RuntimeError, match="synthetic review interruption"):
        await graph.ainvoke(example_state(), config)
    state = await graph.ainvoke(None, config)
    assert len(state["investigations"]) == 1
    assert state["reviews"][-1].decision == "PASS"


@pytest.mark.asyncio
async def test_changed_source_version_is_not_silently_assigned():
    tools = DemoTools()
    tools.rows[0]["metadata"]["source_version"] = "v1"
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    tools.rows[0]["metadata"]["source_version"] = "v2"
    assert (await completion_payload(state, runtime))["review_reason"] == "VALIDATION_FAILED"
