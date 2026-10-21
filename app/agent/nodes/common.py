import json
from typing import Any

from pydantic import BaseModel

from app.agent.runtime import AgentRuntime
from app.agent.schemas import AgentContext, GraphState, InternalHold, NextStep
from app.agent.tools import ToolGateway, evidence_groups, get_context


def json_context(value: Any) -> str:
    def encode(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        raise TypeError(f"Cannot encode {type(item).__name__}")

    return json.dumps(value, ensure_ascii=False, default=encode)


def hold_update(
    context: AgentContext, reason: str, summary: str, questions: list[str] | None = None
) -> dict:
    context.hold = InternalHold(
        reason=reason,
        summary=summary,
        unresolved_questions=questions or [],
    )
    return {"agent_context": context.model_dump(mode="json")}


async def prepare(state: GraphState, runtime: AgentRuntime) -> tuple[AgentContext, ToolGateway]:
    context = get_context(state)
    context.prepared_payload = {}
    context.final_validation = "PENDING"
    context.final_evidence = []
    tools = await runtime.tools("business", "rag")
    gateway = ToolGateway(tools, state, context, runtime.settings)
    result = await gateway.call(
        "get_request_context", {}, "현재 접수·기체·검색 조건 버전이 유효한가?"
    )
    if context.hold is not None:
        return context, gateway
    if result.get("status") != "OK":
        hold_update(context, "VALIDATION_FAILED", "현재 접수 컨텍스트를 확인하지 못했습니다.")
        return context, gateway
    await gateway.call(
        "search_departments",
        {"query": state["symptom_text"][:2000], "limit": 20},
        "현재 접수 범위에서 증상 관련 업무를 맡을 수 있는 부서는 어디인가?",
    )
    return context, gateway


async def select_evidence(
    state: GraphState,
    context: AgentContext,
    gateway: ToolGateway,
    runtime: AgentRuntime,
    system: str,
    stage: str,
) -> NextStep:
    classification = state["classification"]
    latest_review = state["reviews"][-1] if state["reviews"] else None
    reviews = latest_review.model_dump() if isinstance(latest_review, BaseModel) else latest_review
    supplement = reviews.get("supplement_request") if reviews else None
    observations: list[dict] = []
    while gateway.calls < runtime.settings.max_tool_calls:
        payload = {
            "stage": stage,
            "request_id": state["request_id"],
            "aircraft": {
                key: state[key]
                for key in (
                    "tail_number",
                    "aircraft_model",
                    "airline_code",
                )
            },
            "symptom_text": state["symptom_text"],
            "classification": classification,
            "investigations": state["investigations"],
            "latest_supplement_request": supplement,
            "retry_count": state["retry_count"],
            "tools": gateway.catalogue(),
            "evidence_records": context.records,
            "evidence_groups": evidence_groups(context),
            "request_context": context.request_context,
            "snapshots": context.snapshots,
            "tool_traces": context.traces,
            "departments": context.departments,
            "observations": observations,
            "remaining_tool_calls": runtime.settings.max_tool_calls - gateway.calls,
        }
        step = await runtime.model.complete(NextStep, system, json_context(payload))
        if step.action != "CALL":
            return step
        try:
            args = json.loads(step.arguments_json)
            if not isinstance(args, dict):
                raise ValueError("Arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError):
            # Count invalid choices too so a malformed planner cannot spin forever.
            await gateway.call("INVALID_ARGUMENT_JSON", {}, step.question)
            observations.append({"tool": step.tool_name, "error": "INVALID_ARGUMENT_JSON"})
            continue
        result = await gateway.call(step.tool_name, args, step.question)
        observations.append({"tool": step.tool_name, "question": step.question, "result": result})
        if context.hold is not None:
            return NextStep(
                action="HOLD",
                question=context.hold.summary,
                tool_name=None,
                arguments_json=None,
                hold_reason="INSUFFICIENT_INFORMATION",
            )
    return NextStep(
        action="HOLD",
        question="도구 호출 한도 안에 조사를 마치지 못했습니다.",
        tool_name=None,
        arguments_json=None,
        hold_reason="INSUFFICIENT_INFORMATION",
    )
