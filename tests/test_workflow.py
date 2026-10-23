import copy
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.nodes.investigate import run_investigate
from app.agent.nodes.review import run_review
from app.agent.runtime import AgentRuntime
from app.agent.schemas import (
    AgentContext,
    Evidence,
    InvestigationAssessment,
    NextStep,
    Review,
    ReviewAssessment,
)
from app.agent.settings import Settings
from app.agent.tools import EvidenceValidationError
from app.agent.workflow import build_graph, completion_payload
from tests.support import DemoTools, ScriptedDemoModel, example_state


@pytest.mark.asyncio
async def test_full_pass_requires_fresh_document_validation():
    tools = DemoTools()
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    initial = example_state()
    saved_initial = copy.deepcopy(initial)
    state = await build_graph(runtime).ainvoke(initial)
    assert initial == saved_initial
    payload = await completion_payload(state, runtime)
    assert payload["outcome"] == "ASSIGN"
    assert payload["department"] == "HYD"
    assert any(name == "get_evidence" for name, _ in tools.calls)
    assert state["agent_context"]["final_validation"] == "PASS"
    # Revalidation comes last; only the post-decision AMM reference lookup follows it.
    tools_called = [t["tool"] for t in state["agent_context"]["traces"]]
    assert tools_called[-2:] == ["get_evidence", "get_sdr_amm_references"]
    assert payload["reference_amm"][0]["amm_ref"] == "32-XX-XX"
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

    def remove(name, args, result):
        if name == "get_evidence":
            result.update(status="NO_MATCH", result=None)
        return result

    tools.on_call = remove
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    payload = await completion_payload(state, runtime)
    assert payload["outcome"] == "HUMAN_REVIEW"
    assert payload["review_reason"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_inactive_department_cannot_be_assigned_from_stale_directory():
    tools = DemoTools()

    def deactivate(name, args, result):
        if name == "search_departments" and sum(n == name for n, _ in tools.calls) == 3:
            result["items"][0]["active"] = False
        return result

    tools.on_call = deactivate
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    assert (await completion_payload(state, runtime))["review_reason"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_fabricated_evidence_goes_to_validation_failed_not_pass():
    class FabricatingModel(ScriptedDemoModel):
        async def complete(self, schema, system, context):
            output = await super().complete(schema, system, context)
            if schema is InvestigationAssessment:
                output.investigation.evidence[0].ref_id = "EV-SDR-INVENTED"
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

    def change_version(name, args, result):
        if name == "get_evidence":
            result["result"]["source_version"] = "DEMO-v2"
        return result

    tools.on_call = change_version
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    assert (await completion_payload(state, runtime))["review_reason"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_amm_is_never_evidence_but_attached_as_reference():
    class ReviewAddsEvidence(ScriptedDemoModel):
        async def complete(self, schema, system, context):
            data = json.loads(context)
            if (
                schema is NextStep
                and data["stage"] == "REVIEWING"
                and not any(r["source"] == "AMM" for r in data["evidence_records"])
            ):
                return NextStep(
                    action="CALL",
                    question="부서 검토에 필요한 인용 맥락은?",
                    tool_name="search_amm_references",
                    arguments_json='{"query":"vibration"}',
                    hold_reason=None,
                )
            result = await super().complete(schema, system, context)
            if schema is ReviewAssessment:
                result.additional_evidence = [
                    Evidence(
                        source="AMM",
                        ref_id="EV-AMM-DEMO-001",
                        excerpt="SYNTHETIC AMM citation context only.",
                        why="추가 검토 근거",
                    )
                ]
            return result

    tools = DemoTools()
    runtime = AgentRuntime(model=ReviewAddsEvidence(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    payload = await completion_payload(state, runtime)
    assert payload["outcome"] == "ASSIGN"
    # The review's AMM citation is dropped from evidence and never revalidated as evidence.
    assert {e["ref_id"] for e in payload["evidence"]} == {"EV-SDR-DEMO-001"}
    assert {a["evidence_id"] for n, a in tools.calls if n == "get_evidence"} == {"EV-SDR-DEMO-001"}
    # R03 is not offered to the planner, so the review's search request never reaches the server.
    assert not any(n == "search_amm_references" for n, _ in tools.calls)
    assert [a["evidence_ids"] for n, a in tools.calls if n == "get_sdr_amm_references"] == [
        ["EV-SDR-DEMO-001"]
    ]
    assert payload["reference_amm"] == [
        {
            "sdr_ref_id": "EV-SDR-DEMO-001",
            "amm_ref": "32-XX-XX",
            "reference_level": "chapter_section",
            "context": "SYNTHETIC AMM citation context only.",
            "evidence_id": "EV-AMM-DEMO-001",
        }
    ]


@pytest.mark.asyncio
async def test_final_revalidation_traces_are_checkpointed():
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=DemoTools())
    graph = build_graph(runtime, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "DEMO-REQUEST-001"}}
    await graph.ainvoke(example_state(), config)
    context = graph.get_state(config).values["agent_context"]
    assert context["final_validation"] == "PASS"
    assert [t["tool"] for t in context["traces"]][-2:] == ["get_evidence", "get_sdr_amm_references"]
    assert context["prepared_payload"]["outcome"] == "ASSIGN"


@pytest.mark.asyncio
async def test_technical_decisive_basis_keeps_public_source_enum_and_hands_off():
    class TechnicalBasis(ScriptedDemoModel):
        async def complete(self, schema, system, context):
            result = await super().complete(schema, system, context)
            if schema is ReviewAssessment:
                result.technical_basis_ids = ["EV-TECH-DEMO-001"]
            return result

    runtime = AgentRuntime(model=TechnicalBasis(), tools=DemoTools())
    state = await build_graph(runtime).ainvoke(example_state())
    assert state["agent_context"]["hold"]["reason"] == "UNSUPPORTED_EVIDENCE_SOURCE"
    assert (await completion_payload(state, runtime))["outcome"] == "HUMAN_REVIEW"


@pytest.mark.asyncio
async def test_context_version_change_at_final_gate_cannot_assign_old_plan():
    tools = DemoTools()

    def change(name, args, result):
        # B01 runs at the start of the investigation and at the final gate (call 2), not per stage.
        if name == "get_request_context" and sum(n == name for n, _ in tools.calls) == 2:
            tools.version = 2
            result["context_version"] = 2
            result["result"]["context_version"] = 2
        return result

    tools.on_call = change
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    assert state["agent_context"]["context_refresh_count"] == 1
    assert state["agent_context"]["context_version"] == 2
    assert len(state["agent_context"]["superseded_assessments"]) == 1
    assert len(state["investigations"]) == 1
    assert sum(s == "InvestigationAssessment" for s, _ in runtime.model.calls) == 2
    assert (await completion_payload(state, runtime))["outcome"] == "ASSIGN"


@pytest.mark.asyncio
async def test_human_or_terminal_request_is_never_automatically_reassigned():
    tools = DemoTools()
    tools.status = "HUMAN_REVIEW"
    model = ScriptedDemoModel()
    runtime = AgentRuntime(model=model, tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    assert model.calls == []
    assert state["agent_context"]["hold"]["reason"] == "REQUEST_STOPPED"
    assert (await completion_payload(state, runtime))["outcome"] == "HUMAN_REVIEW"


@pytest.mark.asyncio
async def test_final_context_read_failure_never_reuses_stale_context():
    tools = DemoTools()

    def fail(name, args, result):
        # call 2 is the final gate
        if name == "get_request_context" and sum(n == name for n, _ in tools.calls) >= 2:
            return {"status": "ERROR", "error": {"code": "OFFLINE", "retryable": False}}
        return result

    tools.on_call = fail
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    assert (await completion_payload(state, runtime))["review_reason"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_repeated_context_changes_stop_after_one_context_refresh():
    tools = DemoTools()

    def change(name, args, result):
        # every second call is a final gate
        if name == "get_request_context" and sum(n == name for n, _ in tools.calls) % 2 == 0:
            tools.version += 1
            result["context_version"] = tools.version
            result["result"]["context_version"] = tools.version
        return result

    tools.on_call = change
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    assert state["agent_context"]["context_refresh_count"] == 1
    assert (await completion_payload(state, runtime))["outcome"] == "HUMAN_REVIEW"


@pytest.mark.asyncio
async def test_history_decision_is_revalidated_through_b02_not_r05():
    class HistoryModel(ScriptedDemoModel):
        async def complete(self, schema, system, context):
            result = await super().complete(schema, system, context)
            if schema is ReviewAssessment:
                result.additional_evidence = [
                    Evidence(
                        source="HISTORY",
                        ref_id="MH-000123",
                        excerpt="inspection requested",
                        why="보고된 조사 이력",
                    )
                ]
            return result

    tools = DemoTools()
    runtime = AgentRuntime(model=HistoryModel(), tools=tools)
    # HISTORY was retrieved by LLM1 before entering the investigation graph.
    from app.agent.nodes.common import prepare

    initial = example_state()
    context, gateway = await prepare(initial, runtime)
    await gateway.call("get_aircraft_history", {}, "LLM1에서 확보한 원자료")
    initial["agent_context"] = context.model_dump(mode="json")
    state = await build_graph(runtime).ainvoke(initial)
    assert "MH-000123" in {
        e["ref_id"] for e in (await completion_payload(state, runtime))["evidence"]
    }
    assert all(e.source != "HISTORY" for e in state["investigations"][-1].evidence)
    assert sum(n == "get_aircraft_history" for n, _ in tools.calls) == 2
    assert all(
        args.get("evidence_id") != "MH-000123"
        for name, args in tools.calls
        if name == "get_evidence"
    )


@pytest.mark.asyncio
async def test_amm_reference_failure_never_blocks_assignment():
    tools = DemoTools()

    def broken(name, args, result):
        if name == "get_sdr_amm_references":
            raise RuntimeError("reference server down")

    tools.on_call = broken
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    payload = await completion_payload(state, runtime)
    assert payload["outcome"] == "ASSIGN" and "reference_amm" not in payload
    assert state["agent_context"]["traces"][-1]["outcome"] == "ERROR"


@pytest.mark.asyncio
async def test_request_context_is_read_at_start_and_final_gate_only():
    tools = DemoTools()
    runtime = AgentRuntime(model=ScriptedDemoModel(), tools=tools)
    state = await build_graph(runtime).ainvoke(example_state())
    assert (await completion_payload(state, runtime))["outcome"] == "ASSIGN"
    # investigation start + final gate; the review stage reuses the confirmed context
    assert sum(n == "get_request_context" for n, _ in tools.calls) == 2
