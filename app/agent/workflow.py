"""LLM2/3 subgraph only. Spring and the LLM1 runner remain external owners."""

from dataclasses import replace

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.common import hold_update
from app.agent.nodes.investigate import run_investigate
from app.agent.nodes.review import run_review
from app.agent.runtime import AgentRuntime
from app.agent.schemas import GraphState, Investigation, Review
from app.agent.tools import (
    EvidenceValidationError,
    ToolGateway,
    evidence_text,
    get_context,
    validate_department,
    validate_evidence,
)


def latest_review(state: GraphState) -> Review:
    value = state["reviews"][-1]
    return value if isinstance(value, Review) else Review.model_validate(value)


def latest_investigation(state: GraphState) -> Investigation:
    value = state["investigations"][-1]
    return value if isinstance(value, Investigation) else Investigation.model_validate(value)


def build_graph(runtime: AgentRuntime, checkpointer=None):
    """Entry assumes backend validation and LLM1's investigation routing are complete."""
    graph = StateGraph(GraphState)

    async def investigate_node(state: GraphState) -> dict:
        return await run_investigate(state, runtime)

    async def review_node(state: GraphState) -> dict:
        return await run_review(state, runtime)

    def route(state: GraphState) -> str:
        result = latest_review(state)
        if result.decision != "SUPPLEMENT":
            return "end"
        return "retry" if state["retry_count"] < runtime.settings.max_reinvestigation else "limit"

    def increment(state: GraphState) -> dict:
        return {"retry_count": state["retry_count"] + 1}

    def retry_limit(state: GraphState) -> dict:
        return hold_update(get_context(state), "RETRY_LIMIT", "재조사 한도에 도달했습니다.")

    graph.add_node("investigate", investigate_node)
    graph.add_node("review", review_node)
    graph.add_node("increment_retry", increment)
    graph.add_node("retry_limit", retry_limit)
    graph.add_edge(START, "investigate")
    graph.add_edge("investigate", "review")
    graph.add_conditional_edges(
        "review",
        route,
        {
            "end": END,
            "retry": "increment_retry",
            "limit": "retry_limit",
        },
    )
    graph.add_edge("increment_retry", "investigate")
    graph.add_edge("retry_limit", END)
    return graph.compile(checkpointer=checkpointer)


def human_payload(reason: str, summary: str) -> dict:
    return {"outcome": "HUMAN_REVIEW", "review_reason": reason, "summary": summary}


async def completion_payload(state: GraphState, runtime: AgentRuntime) -> dict:
    """Prepare the PDF's COMPLETED payload; does not send it or assign a request.

    The caller must use this after the subgraph and before sending COMPLETED. A PASS
    alone is insufficient. Re-read every cited record and the current department list.
    """
    context = get_context(state)
    if context.hold is not None:
        reason = context.hold.reason
        if reason not in {"RETRY_LIMIT", "VALIDATION_FAILED"}:
            reason = "AGENT_NEEDS_HUMAN"
        return human_payload(reason, context.hold.summary)
    review = latest_review(state)
    if review.decision == "HUMAN":
        return human_payload("AGENT_NEEDS_HUMAN", review.comments)
    if review.decision != "PASS":
        raise ValueError("The graph has not reached a final review outcome")
    investigation = latest_investigation(state)
    tools = await runtime.tools("business", "rag")
    unique = {(item.source, item.ref_id) for item in investigation.evidence}
    if len(unique) > 20:
        return human_payload("VALIDATION_FAILED", "최종 근거 재검증 상한 20건을 초과했습니다.")
    validation_settings = replace(runtime.settings, max_tool_calls=len(unique) + 1)
    gateway = ToolGateway(tools, state, context, validation_settings)
    await gateway.call("list_departments", {}, "배정 직전에도 제안 부서가 유효한가?")
    try:
        validate_department(review.department, context)
        validate_evidence(investigation.evidence, context)
        # These are mandatory validation reads, not discretionary investigation calls.
        checked: set[tuple[str, str]] = set()
        for evidence in investigation.evidence:
            key = (evidence.source, evidence.ref_id)
            if key in checked:
                continue
            checked.add(key)
            if evidence.source in {"SDR", "AMM"}:
                result = await gateway.call(
                    "get_document",
                    {"id": evidence.ref_id},
                    "인용한 문서 ID와 원문이 현재도 일치하는가?",
                )
            else:
                trace = next(
                    (
                        trace
                        for trace in reversed(context.traces)
                        if trace.tool in {"get_maintenance_history", "search_similar_history"}
                        and evidence.ref_id in trace.item_ids
                    ),
                    None,
                )
                if trace is None:
                    raise EvidenceValidationError("No original history retrieval for cited id")
                result = await gateway.call(
                    trace.tool, trace.arguments, "인용한 정비 이력을 재확인할 수 있는가?"
                )
            matches = [
                item for item in result.get("items", []) if item.get("id") == evidence.ref_id
            ]
            if result.get("isError") or not matches:
                raise EvidenceValidationError("Cited evidence could not be re-read")
            if evidence.excerpt not in (evidence_text(matches[0]) or ""):
                raise EvidenceValidationError("Cited excerpt changed or disappeared")
        validate_evidence(investigation.evidence, context)
    except EvidenceValidationError as exc:
        return human_payload("VALIDATION_FAILED", str(exc))
    return {
        "outcome": "ASSIGN",
        "department": review.department,
        "summary": f"{investigation.finding}\n{review.comments}",
        "evidence": [item.model_dump(mode="json") for item in investigation.evidence],
    }
