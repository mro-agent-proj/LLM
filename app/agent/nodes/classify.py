"""LLM1: read business MCP, classify, then validate the model's references."""

from app.agent.classification import CLASSIFY_SYSTEM, ClassificationDecision
from app.agent.mcp_client import BUSINESS_TOOLS
from app.agent.nodes.common import hold_update, json_context
from app.agent.runtime import AgentRuntime, get_runtime
from app.agent.schemas import AgentContext, Classification, Evidence, GraphState
from app.agent.tools import EvidenceValidationError, ToolGateway, validate_department


def validate_classification(output: Classification, context: AgentContext) -> None:
    ids = output.linked_history_ids
    if len(ids) != len(set(ids)) or len(ids) > 20:
        raise EvidenceValidationError("연결 이력은 중복 없이 최대 20건이어야 합니다.")
    history = {r.ref_id: r for r in context.records if r.source == "HISTORY"}
    if any(identifier not in history for identifier in ids):
        raise EvidenceValidationError("조회되지 않은 이력이 분류 결과에 포함됐습니다.")
    if output.proposed_department is not None:
        validate_department(output.proposed_department, context)
    if output.sufficient:
        if not ids:
            raise EvidenceValidationError("충분한 분류에는 동일 기체 연결 이력이 필요합니다.")
        department = next(d for d in context.departments if d["code"] == output.proposed_department)
        aliases = {department["code"], department.get("department_id")} - {None}
        if not any(
            history[identifier].metadata.get("department_id") in aliases
            or history[identifier].metadata.get("department_code") in aliases
            for identifier in ids
        ):
            raise EvidenceValidationError("연결 이력의 담당 부서와 현재 부서를 확인할 수 없습니다.")


async def business_gateway(state: GraphState, context: AgentContext, runtime: AgentRuntime):
    tools = await runtime.tools("business")
    selected = [tool for tool in tools if tool.name in BUSINESS_TOOLS]
    if {tool.name for tool in selected} != BUSINESS_TOOLS:
        raise RuntimeError("LLM1 requires B01, B02 and B03")
    return ToolGateway(selected, state, context, runtime.settings)


async def run_classify(state: GraphState, runtime: AgentRuntime) -> dict:
    # Start a fresh classification; no old run's evidence, payload or tokens are reused.
    context = AgentContext(request_id=state["request_id"])
    gateway = await business_gateway(state, context, runtime)

    def stop(reason: str, summary: str) -> dict:
        if context.hold is None:
            hold_update(context, reason, summary)
        return {"classification": None, "agent_context": context.model_dump(mode="json")}

    response = await gateway.call("get_request_context", {}, "접수 기체·증상·권한 범위를 확인한다.")
    if context.hold or response.get("status") != "OK":
        return stop("VALIDATION_FAILED", "접수 정보를 확인하지 못했습니다.")
    aircraft_id = (context.request_context.get("aircraft") or {}).get("aircraft_id")
    if not aircraft_id:
        return stop("VALIDATION_FAILED", "동일 기체를 대조할 aircraft_id가 없습니다.")
    response = await gateway.call(
        "search_departments", {"limit": 20}, "현재 담당 부서 목록을 읽는다."
    )
    if context.hold or response.get("status") not in {"OK", "NO_DATA", "NO_MATCH"}:
        return stop("VALIDATION_FAILED", "부서 조회에 실패했습니다.")
    if not context.departments:
        return stop("DEPARTMENT_UNAVAILABLE", "담당 가능한 부서가 없습니다.")

    # No symptom query/date cutoff: do not hide a related but differently worded history.
    arguments = {"limit": 100}
    seen_cursors: set[str] = set()
    while True:
        if gateway.calls >= runtime.settings.max_tool_calls:
            return stop("TOOL_LIMIT", "기체 이력의 나머지 페이지를 확인하지 못했습니다.")
        response = await gateway.call(
            "get_aircraft_history", arguments, "동일 기체 이력을 비교한다."
        )
        if context.hold or response.get("status") not in {"OK", "NO_DATA", "NO_MATCH"}:
            return stop("VALIDATION_FAILED", "기체 이력 조회에 실패했습니다.")
        for record in context.records:
            if record.source != "HISTORY" or record.metadata.get("aircraft_id") != aircraft_id:
                return stop("VALIDATION_FAILED", "이력의 동일 기체 소속을 검증하지 못했습니다.")
        cursor = response.get("next_cursor")
        if not cursor:
            break
        if cursor in seen_cursors:
            return stop("VALIDATION_FAILED", "이력 페이지 커서가 반복됐습니다.")
        seen_cursors.add(cursor)
        arguments = {"limit": 100, "cursor": cursor}

    decision = await runtime.model.complete(
        ClassificationDecision,
        CLASSIFY_SYSTEM,
        json_context(
            {
                "request": {
                    k: state[k]
                    for k in (
                        "request_id",
                        "tail_number",
                        "aircraft_model",
                        "airline_code",
                        "symptom_text",
                    )
                },
                "request_context": context.request_context,
                "history": sorted(
                    context.records,
                    key=lambda r: str(
                        r.metadata.get("occurred_at") or r.metadata.get("reported_at") or ""
                    ),
                    reverse=True,
                ),
                "departments": context.departments,
            }
        ),
    )
    if decision.classification is None:
        return stop("INSUFFICIENT_INFORMATION", decision.missing_information)
    output = decision.classification
    try:
        validate_classification(output, context)
    except EvidenceValidationError as exc:
        return stop("VALIDATION_FAILED", str(exc))
    if output.sufficient:
        records = {r.ref_id: r for r in context.records}
        context.final_evidence = [
            Evidence(
                source="HISTORY",
                ref_id=identifier,
                excerpt=records[identifier].content[:300],
                why=output.rationale,
            )
            for identifier in output.linked_history_ids
        ]
    return {"classification": output, "agent_context": context.model_dump(mode="json")}


async def classify(state: GraphState) -> dict:
    return await run_classify(state, get_runtime())
