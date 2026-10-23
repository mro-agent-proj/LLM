"""Separate LangGraph channels: only the review join receives both agents' material."""

import json
from copy import deepcopy

from app.agent.nodes.common import hold_update
from app.agent.nodes.investigate import run_investigate
from app.agent.runtime import AgentRuntime
from app.agent.schemas import AgentContext, GraphState, Investigation, InvestigatorState
from app.agent.tools import get_context
from app.messages import stage_event


async def run_isolated_investigation(state: InvestigatorState, runtime: AgentRuntime) -> dict:
    # This function is a LangGraph node with InvestigatorState as its input schema.
    # No classification, global agent_context, reviews, or public stage_events can enter.
    context = AgentContext.model_validate(
        state.get("llm2_context") or {"request_id": state["request_id"]}
    )
    if (
        context.request_id != state["request_id"]
        or any(r.source == "HISTORY" for r in context.records)
        or any(t.tool == "get_aircraft_history" for t in context.traces)
        or context.classification_comparisons
        or context.review_comparisons
    ):
        raise ValueError("LLM2 checkpoint contains foreign agent material")
    # Never pass a prior reviewer free-text supplement: it can quote LLM1 history.
    # Retries keep only LLM2's own findings and a generic independent recheck instruction.
    feedback = (
        [
            {
                "supplement_request": (
                    "원본 신고와 직접 조회한 RAG 근거로 "
                    "이전 조사안을 독립 재검토하세요."
                )
            }
        ]
        if state["retry_count"]
        else []
    )
    local: GraphState = {
        **{
            k: deepcopy(state[k])
            for k in (
                "request_id",
                "tail_number",
                "aircraft_model",
                "airline_code",
                "symptom_text",
                "investigations",
                "retry_count",
            )
        },
        "classification": None,
        "reviews": feedback,
        "agent_context": context.model_dump(mode="json"),
    }
    if any(
        e.source == "HISTORY"
        for i in local["investigations"]
        for e in Investigation.model_validate(i).evidence
    ):
        raise ValueError("Legacy HISTORY investigation must not resume in isolated LLM2")
    update = await run_investigate(local, runtime)
    result = {"llm2_context": update["agent_context"]}
    if "investigations" in update:
        result["investigations"] = update["investigations"]
    return result


def merge_for_review(state: GraphState) -> dict:
    """Join LLM1/reviewer context with LLM2's private context outside the LLM2 node."""
    original = get_context(state)
    incoming = AgentContext.model_validate(state["llm2_context"])
    if original.request_id != incoming.request_id:
        raise ValueError("Review join request mismatch")
    for field in ("context_version", "applied_scope", "snapshots"):
        before, after = getattr(original, field), getattr(incoming, field)
        if before and after and before != after:
            return hold_update(
                original, "CONTEXT_CHANGED", "판단 단계 사이 요청 범위·버전이 변경됐습니다."
            )
    for field in (
        "context_version",
        "request_context",
        "applied_scope",
        "snapshots",
        "departments",
    ):
        if getattr(incoming, field):
            setattr(original, field, deepcopy(getattr(incoming, field)))
    original.records = list(
        {(r.source, r.ref_id): r for r in [*original.records, *incoming.records]}.values()
    )
    original.traces = list(
        {
            json.dumps(t.model_dump(mode="json"), sort_keys=True): t
            for t in [*original.traces, *incoming.traces]
        }.values()
    )
    original.cursors.update(deepcopy(incoming.cursors))
    original.jasc_candidates = deepcopy(incoming.jasc_candidates)
    original.hold = incoming.hold
    original.final_validation = "PENDING"
    original.final_evidence = []
    original.prepared_payload = {}
    result = {"agent_context": original.model_dump(mode="json")}
    if not incoming.hold and state["investigations"]:
        output = state["investigations"][-1]
        output = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
        result.update(stage_event(state, "INVESTIGATING", output))
    return result
