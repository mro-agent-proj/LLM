"""Investigate a defect, review its handoff, and revisit unresolved evidence."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.common import hold_update
from app.agent.nodes.investigate import run_investigate
from app.agent.nodes.review import run_review
from app.agent.runtime import AgentRuntime
from app.agent.schemas import GraphState, Investigation, Review
from app.agent.tools import (
    EvidenceValidationError,
    ToolGateway,
    get_context,
    reference_amm,
    validate_department,
    validate_department_ata,
    validate_evidence,
)
from app.messages import event, stage_event


def latest_review(state: GraphState) -> Review:
    value = state["reviews"][-1]
    return value if isinstance(value, Review) else Review.model_validate(value)


def latest_investigation(state: GraphState) -> Investigation:
    value = state["investigations"][-1]
    return value if isinstance(value, Investigation) else Investigation.model_validate(value)


def build_graph(runtime: AgentRuntime, checkpointer=None):
    """Run a validated maintenance request through investigation and independent review."""
    graph = StateGraph(GraphState)

    async def investigate_node(state: GraphState) -> dict:
        update = await run_investigate(state, runtime)
        if len(update.get("investigations", [])) > len(state["investigations"]):
            output = update["investigations"][-1].model_dump(mode="json")
            update.update(stage_event(state, "INVESTIGATING", output))
        return update

    async def review_node(state: GraphState) -> dict:
        update = await run_review(state, runtime)
        output = update["reviews"][-1].model_dump(mode="json")
        update.update(stage_event(state, "REVIEWING", output))
        return update

    def complete_node(state: GraphState) -> dict:
        payload = get_context(state).prepared_payload
        return {
            "stage_events": [
                *state.get("stage_events", []),
                event(state["request_id"], "COMPLETED", payload),
            ]
        }

    async def finalize_node(state: GraphState) -> dict:
        return await finalize_state(state, runtime)

    def route(state: GraphState) -> str:
        context = get_context(state)
        if (
            context.hold
            and context.hold.reason == "CONTEXT_CHANGED"
            and context.context_refresh_count < 1
        ):
            return "refresh"
        result = latest_review(state)
        if result.decision != "SUPPLEMENT":
            return "end"
        return "retry" if state["retry_count"] < runtime.settings.max_reinvestigation else "limit"

    def increment(state: GraphState) -> dict:
        return {"retry_count": state["retry_count"] + 1}

    def retry_limit(state: GraphState) -> dict:
        return hold_update(get_context(state), "RETRY_LIMIT", "재조사 한도에 도달했습니다.")

    def refresh_context(state: GraphState) -> dict:
        context = get_context(state)
        context.superseded_assessments.append(
            {
                "context_version": context.context_version,
                "classification": state["classification"],
                "investigations": state["investigations"],
                "reviews": state["reviews"],
            }
        )
        context.context_refresh_count += 1
        context.context_version = None
        context.request_context = {}
        context.applied_scope = {}
        context.snapshots = {}
        context.classification_comparisons = []
        context.review_comparisons = []
        context.history_focus_ids = []
        context.cross_model_reviews = []
        context.hold = None
        context.prepared_payload = {}
        context.final_validation = "PENDING"
        return {
            "classification": None,
            "investigations": [],
            "reviews": [],
            "agent_context": context.model_dump(mode="json"),
        }

    def after_finalization(state: GraphState) -> str:
        context = get_context(state)
        return (
            "refresh"
            if context.hold
            and context.hold.reason == "CONTEXT_CHANGED"
            and context.context_refresh_count < 1
            else "end"
        )

    graph.add_node("investigate", investigate_node)
    graph.add_node("investigation_started", lambda s: stage_event(s, "INVESTIGATING"))
    graph.add_node("review_started", lambda s: stage_event(s, "REVIEWING"))
    graph.add_node("review", review_node)
    graph.add_node("increment_retry", increment)
    graph.add_node("retry_limit", retry_limit)
    graph.add_node("finalize", finalize_node)
    graph.add_node("refresh_context", refresh_context)
    graph.add_node("completed", complete_node)
    graph.add_edge(START, "investigation_started")
    graph.add_edge("investigation_started", "investigate")
    graph.add_edge("investigate", "review_started")
    graph.add_edge("review_started", "review")
    graph.add_conditional_edges(
        "review",
        route,
        {
            "end": "finalize",
            "retry": "increment_retry",
            "limit": "retry_limit",
            "refresh": "refresh_context",
        },
    )
    graph.add_edge("increment_retry", "investigation_started")
    graph.add_edge("retry_limit", "finalize")
    graph.add_edge("refresh_context", "investigation_started")
    graph.add_conditional_edges(
        "finalize", after_finalization, {"refresh": "refresh_context", "end": "completed"}
    )
    graph.add_edge("completed", END)
    return graph.compile(checkpointer=checkpointer)


def human_payload(reason: str, summary: str) -> dict:
    return {"outcome": "HUMAN_REVIEW", "review_reason": reason, "summary": summary}


async def finalize_state(state: GraphState, runtime: AgentRuntime) -> dict:
    """Graph node: revalidate all decision evidence and checkpoint the audit/payload."""
    context = get_context(state)
    payload = None
    if context.hold is None and latest_review(state).decision == "PASS":
        investigation = latest_investigation(state)
        evidence = context.final_evidence or investigation.evidence
        unique = {(e.source, e.ref_id) for e in evidence}
        if len(unique) > 20:
            hold_update(context, "VALIDATION_FAILED", "최종 근거 재검증 상한 20건을 초과했습니다.")
        else:
            gateway = ToolGateway(
                await runtime.tools("business", "rag"),
                state,
                context,
                replace(runtime.settings, max_tool_calls=len(unique) + 2),
            )
            request_result = await gateway.call(
                "get_request_context",
                {},
                "최종 결정 직전에도 접수·검색 범위 버전과 상태가 유효한가?",
            )
            if request_result.get("status") != "OK" and context.hold is None:
                hold_update(
                    context, "VALIDATION_FAILED", "최종 접수 상태·검색 범위를 확인하지 못했습니다."
                )
            if context.hold is None:
                result = await gateway.call(
                    "search_departments",
                    {"query": state["symptom_text"][:2000], "limit": 20},
                    "검토한 부서가 현재도 이 접수의 업무를 수락할 수 있는가?",
                )
                if result.get("status") != "OK":
                    hold_update(context, "VALIDATION_FAILED", "최종 부서 조회에 실패했습니다.")
            try:
                if context.hold is not None:
                    raise EvidenceValidationError(context.hold.summary)
                validate_department(latest_review(state).department, context)
                validate_department_ata(
                    latest_review(state).department, investigation.ata_chapter, context
                )
                validate_evidence(evidence, context)
                checked = set()
                for item in evidence:
                    key = (item.source, item.ref_id)
                    if key in checked:
                        continue
                    checked.add(key)
                    if item.source != "HISTORY":
                        result = await gateway.call(
                            "get_evidence",
                            {"evidence_id": item.ref_id},
                            "최종 결정에 인용한 근거의 출처·버전·원문 범위가 유지되는가?",
                        )
                        returned = result.get("result") or {}
                        matched = returned.get("evidence_id") == item.ref_id
                    else:
                        trace = next(
                            (
                                t
                                for t in reversed(context.traces)
                                if t.tool == "get_aircraft_history"
                                and any(
                                    row.get("history_id", row.get("id")) == item.ref_id
                                    for row in t.response.get("items", [])
                                )
                            ),
                            None,
                        )
                        if trace is None:
                            raise EvidenceValidationError("No original B02 retrieval for HISTORY")
                        result = await gateway.call(
                            "get_aircraft_history",
                            trace.arguments,
                            "인용한 동일 기체 이력과 조치·결과를 다시 확인할 수 있는가?",
                        )
                        matched = any(
                            row.get("history_id", row.get("id")) == item.ref_id
                            for row in result.get("items", [])
                        )
                    if result.get("status") != "OK" or not matched or context.hold:
                        raise EvidenceValidationError("Cited evidence could not be revalidated")
                validate_evidence(evidence, context)
                context.final_evidence = evidence
                payload = {
                    "outcome": "ASSIGN",
                    "department": latest_review(state).department,
                    "summary": f"{investigation.finding}\n{latest_review(state).comments}",
                    "evidence": [e.model_dump(mode="json") for e in evidence],
                }
                expanded = sorted(
                    {
                        record.metadata["model_key"]
                        for record in context.records
                        if record.source == "SDR"
                        and any(e.ref_id == record.ref_id for e in evidence)
                        and record.metadata.get("model_key")
                        not in (None, context.applied_scope.get("model_key"))
                    }
                )
                if expanded:
                    payload["summary"] += (
                        "\n[기종 확장 근거: "
                        + ", ".join(expanded)
                        + "; 요청 모델: "
                        + str(context.applied_scope.get("model_key"))
                        + ". 부서 배정 참고 사례이며 "
                        "엔진·부품 구성 및 정비 적용성은 확인되지 않음.]"
                    )
                sdr_ids = sorted({e.ref_id for e in evidence if e.source == "SDR"})[:10]
                if sdr_ids:
                    found = await gateway.reference(
                        "get_sdr_amm_references",
                        {"evidence_ids": sdr_ids},
                        "근거로 쓴 SDR에 인용된 AMM 참고번호는? (정비팀 전달용, 근거 아님)",
                    )
                    references = reference_amm(found, state["request_id"], set(sdr_ids))
                    if references:
                        payload["reference_amm"] = references
            except EvidenceValidationError as exc:
                if context.hold is None:
                    hold_update(context, "VALIDATION_FAILED", str(exc))
    if payload is None:
        if context.hold:
            reason = context.hold.reason
            if reason in {"CONTEXT_CHANGED", "SNAPSHOT_CHANGED"}:
                reason = "VALIDATION_FAILED"
            elif reason not in {"RETRY_LIMIT", "VALIDATION_FAILED"}:
                reason = "AGENT_NEEDS_HUMAN"
            payload = human_payload(reason, context.hold.summary)
        else:
            review = latest_review(state)
            if review.decision != "HUMAN":
                raise ValueError("No final review outcome")
            payload = human_payload("AGENT_NEEDS_HUMAN", review.comments)
    context.final_validation = "PASS" if payload["outcome"] == "ASSIGN" else "HUMAN"
    context.prepared_payload = payload
    context.validated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {"agent_context": context.model_dump(mode="json")}


async def completion_payload(state: GraphState, runtime: AgentRuntime) -> dict:
    """Extract the graph's validated payload. Standalone nodes require finalize_state."""
    context = get_context(state)
    if not context.prepared_payload:
        update = await finalize_state(state, runtime)
        context = get_context({**state, **update})
    return deepcopy(context.prepared_payload)
