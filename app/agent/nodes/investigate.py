from app.agent.nodes.common import (
    citable,
    cited_history,
    hold_update,
    json_context,
    model_records,
    model_traces,
    prepare,
    select_evidence,
)
from app.agent.prompts import LLM2_SYSTEM
from app.agent.runtime import AgentRuntime, get_runtime
from app.agent.schemas import GraphState, InvestigationAssessment
from app.agent.tools import (
    EvidenceValidationError,
    ground_evidence,
    validate_department,
    validate_evidence,
)


async def run_investigate(state: GraphState, runtime: AgentRuntime) -> dict:
    context, gateway = await prepare(state, runtime, stage="INVESTIGATING")
    if context.hold is not None:
        return {"agent_context": context.model_dump(mode="json")}
    if not context.departments:
        return hold_update(
            context, "DEPARTMENT_UNAVAILABLE", "유효한 부서 목록을 확인하지 못했습니다."
        )
    step = await select_evidence(state, context, gateway, runtime, LLM2_SYSTEM, "INVESTIGATING")
    if step.action == "HOLD":
        if context.hold is not None:
            return {"agent_context": context.model_dump(mode="json")}
        reason = (
            "TOOL_LIMIT" if gateway.calls >= runtime.settings.max_tool_calls else step.hold_reason
        )
        return hold_update(context, reason, step.question, [step.question])
    cited = cited_history(state)
    if not any(r.source != "TECHNICAL_DOC" and citable(r, state, cited) for r in context.records):
        return hold_update(
            context,
            "INSUFFICIENT_EVIDENCE",
            "정상 조사안을 뒷받침할 조회 근거가 없습니다.",
            ["현재 증상을 확인할 이력·사례 또는 현장 정보를 확보해야 합니다."],
        )
    assessment = await runtime.model.complete(
        InvestigationAssessment,
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
                "previous_investigations": state["investigations"],
                "supplement_requests": [
                    (r.model_dump() if hasattr(r, "model_dump") else r).get("supplement_request")
                    for r in state["reviews"]
                ],
                "evidence_records": model_records(context, state),
                "departments": context.departments,
                "request_context": context.request_context,
                "tool_traces": model_traces(context),
                "instruction": "확인된 자료만 사용해 정상 조사안을 작성하세요.",
            }
        ),
    )
    if assessment.technical_basis_ids:
        return hold_update(
            context,
            "UNSUPPORTED_EVIDENCE_SOURCE",
            "공개 기술자료의 현재 기체 적용성을 기술 담당자가 확인해야 합니다.",
            assessment.technical_basis_ids,
        )
    output = assessment.investigation
    # Reject HISTORY even if a model invents/guesses a valid MH identifier.
    if any(e.source == "HISTORY" for e in output.evidence):
        return hold_update(context, "VALIDATION_FAILED", "LLM2 cannot cite aircraft history.")
    # AMM is attached after assignment as a reference, never cited as evidence.
    admissible = [e for e in output.evidence if e.source != "AMM"]
    if not admissible:
        return hold_update(
            context,
            "INSUFFICIENT_EVIDENCE",
            "조사안에 조회한 근거가 없습니다.",
            ["현재 증상과 같은 대상의 SDR 사례를 확보해야 합니다."],
        )
    output = output.model_copy(update={"evidence": ground_evidence(admissible, context)})
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
