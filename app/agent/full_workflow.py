"""LLM1/2/3 orchestration with checkpointed public stage events."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.classify import business_gateway, run_classify, validate_classification
from app.agent.nodes.common import hold_update
from app.agent.nodes.isolation import merge_for_review, run_isolated_investigation
from app.agent.nodes.review import run_review
from app.agent.runtime import AgentRuntime
from app.agent.schemas import Classification, GraphState, InvestigatorState
from app.agent.tools import EvidenceValidationError, get_context, validate_evidence
from app.agent.workflow import finalize_state, human_payload, latest_review
from app.messages import event, stage_event

StageCallback = Callable[[str, str, int, dict | None], Awaitable[None]]


def finish_hold(state: GraphState) -> dict:
    context = get_context(state)
    if context.hold is None:
        raise ValueError("Expected an explicit hold")
    reason = context.hold.reason
    if reason in {"CONTEXT_CHANGED", "SNAPSHOT_CHANGED"}:
        reason = "VALIDATION_FAILED"
    elif reason not in {"RETRY_LIMIT", "VALIDATION_FAILED"}:
        reason = "AGENT_NEEDS_HUMAN"
    context.prepared_payload = human_payload(reason, context.hold.summary)
    context.final_validation = "HUMAN"
    context.validated_at = datetime.now(UTC).isoformat()
    return {"agent_context": context.model_dump(mode="json")}


async def finalize_classification(state: GraphState, runtime: AgentRuntime) -> dict:
    context = get_context(state)
    output = Classification.model_validate(state["classification"])
    if not output.sufficient:
        raise ValueError("Direct finalization requires sufficient=True")
    evidence = list(context.final_evidence)
    original_queries: dict[str, dict] = {}
    try:
        validate_classification(output, context)
        if {e.ref_id for e in evidence} != set(output.linked_history_ids):
            raise EvidenceValidationError("분류 이력과 최종 근거가 다릅니다.")
        for identifier in output.linked_history_ids:
            trace = next(
                (
                    t
                    for t in reversed(context.traces)
                    if t.tool == "get_aircraft_history"
                    and any(
                        row.get("history_id", row.get("id")) == identifier
                        for row in t.response.get("items", [])
                    )
                ),
                None,
            )
            if trace is None:
                raise EvidenceValidationError("원래 이력 조회 기록이 없습니다.")
            original_queries[json.dumps(trace.arguments, sort_keys=True)] = trace.arguments
        bounded = replace(
            runtime, settings=replace(runtime.settings, max_tool_calls=len(original_queries) + 2)
        )
        gateway = await business_gateway(state, context, bounded)
        for name, args in [("get_request_context", {}), ("search_departments", {"limit": 20})]:
            response = await gateway.call(name, args, "직접 배정 제안 직전 접수·부서를 재확인한다.")
            if context.hold or response.get("status") != "OK":
                raise EvidenceValidationError("최종 접수·부서 검증에 실패했습니다.")
        refreshed_ids: set[str] = set()
        for args in original_queries.values():
            response = await gateway.call(
                "get_aircraft_history", args, "연결 이력을 다시 확인한다."
            )
            if context.hold or response.get("status") != "OK":
                raise EvidenceValidationError("최종 이력 조회에 실패했습니다.")
            refreshed_ids.update(
                row.get("history_id", row.get("id")) for row in response.get("items", [])
            )
        if not set(output.linked_history_ids) <= refreshed_ids:
            raise EvidenceValidationError("연결 이력이 최종 조회에서 사라졌습니다.")
        validate_classification(output, context)
        validate_evidence(evidence, context)
    except EvidenceValidationError as exc:
        if context.hold is None:
            hold_update(context, "VALIDATION_FAILED", str(exc))
        return finish_hold({**state, "agent_context": context.model_dump(mode="json")})
    context.prepared_payload = {
        "outcome": "ASSIGN",
        "department": output.proposed_department,
        "summary": output.rationale,
        "evidence": [e.model_dump(mode="json") for e in evidence],
    }
    context.final_validation = "PASS"
    context.validated_at = datetime.now(UTC).isoformat()
    return {"agent_context": context.model_dump(mode="json")}


def build_full_graph(
    runtime: AgentRuntime, *, on_stage: StageCallback | None = None, checkpointer=None
):
    graph = StateGraph(GraphState)

    def observed(fn, stage: str, field: str):
        async def node(state):
            attempt = 1 if stage == "CLASSIFYING" else state["retry_count"] + 1
            if on_stage:
                await on_stage("STAGE_STARTED", stage, attempt, None)
            update = await fn(state, runtime)
            output = update.get(field)
            if isinstance(output, list):
                output = output[-1] if output else None
            if on_stage and output is not None:
                await on_stage("STAGE_RESULT", stage, attempt, output.model_dump(mode="json"))
            if output is not None:
                update.update(stage_event(state, stage, output.model_dump(mode="json")))
            return update

        return node

    def after_classify(state):
        if get_context(state).hold:
            return "hold"
        return (
            "direct"
            if Classification.model_validate(state["classification"]).sufficient
            else "investigate"
        )

    def after_review(state):
        if get_context(state).hold:
            return "hold"
        if latest_review(state).decision == "SUPPLEMENT":
            return (
                "retry" if state["retry_count"] < runtime.settings.max_reinvestigation else "limit"
            )
        return "finalize"

    async def investigate_node(state: InvestigatorState):
        if on_stage:
            await on_stage("STAGE_STARTED", "INVESTIGATING", state["retry_count"] + 1, None)
        update = await run_isolated_investigation(state, runtime)
        if on_stage and update.get("investigations"):
            await on_stage(
                "STAGE_RESULT",
                "INVESTIGATING",
                state["retry_count"] + 1,
                update["investigations"][-1].model_dump(mode="json"),
            )
        return update

    async def direct(state):
        return await finalize_classification(state, runtime)

    async def final(state):
        return await finalize_state(state, runtime)

    def limit(state):
        return hold_update(get_context(state), "RETRY_LIMIT", "재조사 한도에 도달했습니다.")

    def completed(state):
        context = get_context(state)
        if not context.prepared_payload or context.final_validation == "PENDING":
            raise ValueError("Final validation did not complete")
        return {
            "stage_events": [
                *state.get("stage_events", []),
                event(state["request_id"], "COMPLETED", context.prepared_payload),
            ]
        }

    graph.add_node("classification_started", lambda s: stage_event(s, "CLASSIFYING"))
    graph.add_node("investigation_started", lambda s: stage_event(s, "INVESTIGATING"))
    graph.add_node("review_started", lambda s: stage_event(s, "REVIEWING"))
    graph.add_node("classify", observed(run_classify, "CLASSIFYING", "classification"))
    graph.add_node("investigate", investigate_node, input_schema=InvestigatorState)
    graph.add_node("merge_for_review", merge_for_review)
    graph.add_node("review", observed(run_review, "REVIEWING", "reviews"))
    graph.add_node("direct", direct)
    graph.add_node("finalize", final)
    graph.add_node("hold", finish_hold)
    graph.add_node("increment", lambda state: {"retry_count": state["retry_count"] + 1})
    graph.add_node("limit", limit)
    graph.add_node("completed", completed)
    graph.add_edge(START, "classification_started")
    graph.add_edge("classification_started", "classify")
    graph.add_conditional_edges(
        "classify",
        after_classify,
        {"hold": "hold", "direct": "direct", "investigate": "investigation_started"},
    )
    graph.add_edge("investigation_started", "investigate")
    graph.add_edge("investigate", "merge_for_review")
    graph.add_edge("merge_for_review", "review_started")
    graph.add_edge("review_started", "review")
    graph.add_conditional_edges(
        "review",
        after_review,
        {"hold": "hold", "retry": "increment", "limit": "limit", "finalize": "finalize"},
    )
    graph.add_edge("increment", "investigation_started")
    graph.add_edge("limit", "hold")
    for node in ("direct", "finalize", "hold"):
        graph.add_edge(node, "completed")
    graph.add_edge("completed", END)
    return graph.compile(checkpointer=checkpointer)
