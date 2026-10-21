from app.agent.nodes.common import hold_update, json_context, prepare, select_evidence
from app.agent.prompts import LLM2_SYSTEM
from app.agent.runtime import AgentRuntime, get_runtime
from app.agent.schemas import GraphState, Investigation
from app.agent.tools import EvidenceValidationError, validate_department, validate_evidence


async def run_investigate(state: GraphState, runtime: AgentRuntime) -> dict:
    context, gateway = await prepare(state, runtime)
    context.hold = None
    if not context.departments:
        return hold_update(
            context, "DEPARTMENT_UNAVAILABLE", "유효한 부서 목록을 확인하지 못했습니다."
        )
    step = await select_evidence(state, context, gateway, runtime, LLM2_SYSTEM, "INVESTIGATING")
    if step.action == "HOLD":
        reason = (
            "TOOL_LIMIT" if gateway.calls >= runtime.settings.max_tool_calls else step.hold_reason
        )
        return hold_update(context, reason, step.question, [step.question])
    if not context.records:
        return hold_update(
            context,
            "INSUFFICIENT_EVIDENCE",
            "정상 조사안을 뒷받침할 조회 근거가 없습니다.",
            ["현재 증상을 확인할 이력·사례 또는 현장 정보를 확보해야 합니다."],
        )
    output = await runtime.model.complete(
        Investigation,
        LLM2_SYSTEM,
        json_context(
            {
                "request": {
                    key: state[key]
                    for key in (
                        "request_id",
                        "tail_number",
                        "aircraft_model",
                        "airline_code",
                        "symptom_text",
                    )
                },
                "classification": state["classification"],
                "previous_investigations": state["investigations"],
                "previous_reviews": state["reviews"],
                "evidence_records": context.records,
                "departments": context.departments,
                "instruction": "확인된 자료만 사용해 정상 조사안을 작성하세요.",
            }
        ),
    )
    try:
        validate_evidence(output.evidence, context)
        validate_department(output.proposed_department, context)
    except EvidenceValidationError as exc:
        return hold_update(context, "VALIDATION_FAILED", str(exc))
    return {
        "investigations": [*state["investigations"], output],
        "agent_context": context.model_dump(mode="json"),
    }


async def investigate(state: GraphState) -> dict:
    return await run_investigate(state, get_runtime())
