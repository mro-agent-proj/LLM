import json
from typing import Any

from pydantic import BaseModel

from app.agent.runtime import AgentRuntime
from app.agent.schemas import AgentContext, GraphState, InternalHold, NextStep
from app.agent.tools import ToolGateway, get_context


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
    tools = await runtime.tools("business", "rag")
    gateway = ToolGateway(tools, state, context, runtime.settings)
    await gateway.call("list_departments", {}, "현재 등록된 담당 부서와 업무 범위는 무엇인가?")
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
            gateway.calls += 1
            observations.append({"tool": step.tool_name, "error": "INVALID_ARGUMENT_JSON"})
            continue
        if step.tool_name not in gateway.tools:
            gateway.calls += 1
        result = await gateway.call(step.tool_name, args, step.question)
        observations.append({"tool": step.tool_name, "question": step.question, "result": result})
    return NextStep(
        action="HOLD",
        question="도구 호출 한도 안에 조사를 마치지 못했습니다.",
        tool_name=None,
        arguments_json=None,
        hold_reason="INSUFFICIENT_INFORMATION",
    )
