from app.agent.nodes.common import hold_update, json_context, prepare, select_evidence
from app.agent.prompts import LLM3_SYSTEM
from app.agent.runtime import AgentRuntime, get_runtime
from app.agent.schemas import GraphState, Investigation, Review, ReviewAssessment
from app.agent.tools import (
    EvidenceValidationError,
    get_context,
    validate_department,
    validate_evidence,
)


def human_review(state: GraphState, context, summary: str) -> dict:
    output = Review(decision="HUMAN", department=None, comments=summary, supplement_request=None)
    return {
        "reviews": [*state["reviews"], output],
        "agent_context": context.model_dump(mode="json"),
    }


async def run_review(state: GraphState, runtime: AgentRuntime) -> dict:
    context = get_context(state)
    if context.hold is not None:
        # Explicit abstention path: no fake Investigation, department or severity.
        return human_review(state, context, context.hold.summary)
    if not state["investigations"]:
        raise ValueError("LLM3 requires an investigation or an explicit internal hold")
    context, gateway = await prepare(state, runtime)
    if context.hold is not None:
        return human_review(state, context, context.hold.summary)
    if not context.departments:
        update = hold_update(context, "DEPARTMENT_UNAVAILABLE", "담당 부서 목록 확인이 필요합니다.")
        return {**update, **human_review(state, context, context.hold.summary)}
    step = await select_evidence(state, context, gateway, runtime, LLM3_SYSTEM, "REVIEWING")
    if step.action == "HOLD":
        if context.hold is None:
            reason = (
                "TOOL_LIMIT"
                if gateway.calls >= runtime.settings.max_tool_calls
                else "INSUFFICIENT_INFORMATION"
            )
            hold_update(context, reason, step.question, [step.question])
        return human_review(state, context, step.question)
    latest = state["investigations"][-1]
    if not isinstance(latest, Investigation):
        latest = Investigation.model_validate(latest)
    assessment = await runtime.model.complete(
        ReviewAssessment,
        LLM3_SYSTEM,
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
                "latest_investigation": latest,
                "previous_reviews": state["reviews"],
                "evidence_records": context.records,
                "departments": context.departments,
                "request_context": context.request_context,
                "tool_traces": context.traces,
            }
        ),
    )
    output = assessment.review
    if output.decision == "PASS" and assessment.technical_basis_ids:
        hold_update(
            context,
            "UNSUPPORTED_EVIDENCE_SOURCE",
            "공개 기술자료의 현재 기체 적용성에 대한 담당자의 확인이 필요합니다.",
            assessment.technical_basis_ids,
        )
        return human_review(state, context, context.hold.summary)
    try:
        if output.department is not None:
            validate_department(output.department, context)
        if output.decision == "PASS":
            validate_evidence(latest.evidence, context)
            validate_evidence(assessment.additional_evidence, context)
            context.final_evidence = list(
                {
                    (e.source, e.ref_id, e.excerpt): e
                    for e in [*latest.evidence, *assessment.additional_evidence]
                }.values()
            )
            context.final_validation = "PENDING"
    except EvidenceValidationError as exc:
        hold_update(context, "VALIDATION_FAILED", str(exc))
        return human_review(state, context, str(exc))
    return {
        "reviews": [*state["reviews"], output],
        "agent_context": context.model_dump(mode="json"),
    }


async def review(state: GraphState) -> dict:
    return await run_review(state, get_runtime())
