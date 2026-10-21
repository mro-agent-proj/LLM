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


# Model input is bounded: full records and traces stay in AgentContext for validation, but the
# prompt gets compact views. LLM1 reads every history page (often 80+ rows with retrieval
# envelopes); resending all of it with every tool response overflowed the 128k context.
RECORD_KEYS = (
    "source_type", "title", "case_id", "operator_code", "model_key", "reported_jasc", "ata_chapter",
    "jasc_code", "department_id", "evidence_group_id", "difficulty_date", "occurred_at",
    "applicability_status", "relationship_status", "reference_level",
)
RESPONSE_KEYS = (
    "status", "counts", "warnings", "applied_scope", "snapshot_ids", "next_cursor", "error",
    "context_version",
)
MAX_RECORD_TEXT = 1500
MAX_UNCITED_HISTORY = 20


def _plain(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def cited_history(state: GraphState) -> set[str]:
    """History rows the case actually rests on: LLM1 links and evidence cited by investigations."""
    ids = set((_plain(state.get("classification")) or {}).get("linked_history_ids") or [])
    for investigation in state.get("investigations") or []:
        for item in _plain(investigation).get("evidence") or []:
            if item.get("source") == "HISTORY":
                ids.add(item["ref_id"])
    return ids


def model_records(context: AgentContext, state: GraphState) -> list[dict]:
    """Every SDR/AMM/document row; history = cited rows + the most recent others (bounded).

    When LLM1 compared the history and linked nothing, those rows were judged unrelated: showing
    them again let LLM2 cite them instead of searching SDR/AMM, so only cited rows remain.
    """
    cited = cited_history(state)
    uncited = [] if llm1_rejected_history(state) else sorted(
        (r for r in context.records if r.source == "HISTORY" and r.ref_id not in cited),
        key=lambda r: str(r.metadata.get("occurred_at") or r.metadata.get("reported_at") or ""),
        reverse=True,
    )
    keep = cited | {r.ref_id for r in uncited[:MAX_UNCITED_HISTORY]}
    return [
        {
            "source": r.source,
            "ref_id": r.ref_id,
            "content": r.content[:MAX_RECORD_TEXT],
            **{k: r.metadata[k] for k in RECORD_KEYS if r.metadata.get(k) is not None},
        }
        for r in context.records
        if r.source != "HISTORY" or r.ref_id in keep
    ]


def llm1_rejected_history(state: GraphState) -> bool:
    classification = _plain(state.get("classification")) or {}
    return bool(classification) and not classification.get("linked_history_ids")


def citable(record, state: GraphState, cited: set[str]) -> bool:
    """History LLM1 compared and linked nothing from is not evidence for LLM2."""
    if record.source != "HISTORY":
        return True
    return record.ref_id in cited or not llm1_rejected_history(state)


def usable_evidence(context: AgentContext, state: GraphState) -> bool:
    """Something an investigation can cite: a retrieved case or an admissible history row."""
    cited = cited_history(state)
    return any(citable(r, state, cited) for r in context.records)


def hidden_tools(context: AgentContext, state: GraphState, stage: str) -> set[str]:
    """LLM1 already compared every history page; with no linked row there is nothing to re-read."""
    linked = (_plain(state.get("classification")) or {}).get("linked_history_ids") or []
    read_by_llm1 = any(r.source == "HISTORY" for r in context.records)
    if stage == "INVESTIGATING" and not linked and read_by_llm1:
        return {"get_aircraft_history"}
    return set()


def omitted_history(context: AgentContext, state: GraphState) -> int:
    shown = {r["ref_id"] for r in model_records(context, state) if r["source"] == "HISTORY"}
    return sum(r.source == "HISTORY" and r.ref_id not in shown for r in context.records)


def summarize_response(response: dict) -> dict:
    """Envelope without the item bodies; the items themselves are in model_records."""
    if not isinstance(response, dict):
        return {}
    return {k: response[k] for k in RESPONSE_KEYS if k in response}


def model_traces(context: AgentContext) -> list[dict]:
    return [
        {
            "tool": t.tool,
            "question": t.question,
            "arguments": t.arguments,
            "outcome": t.outcome,
            "item_ids": t.item_ids,
            "error_code": t.error_code,
            "response": summarize_response(t.response),
        }
        for t in context.traces
    ]


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
    # B01·B03 are mandatory checks, not the agent's choice: only planner calls use the budget.
    gateway.calls = 0
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
    seen_calls: set[str] = set()
    hidden = hidden_tools(context, state, stage)
    hold_rejected = False
    finish_rejections = 0
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
            "tools": [t for t in gateway.catalogue() if t["name"] not in hidden],
            "evidence_records": model_records(context, state),
            "omitted_history_count": omitted_history(context, state),
            "evidence_groups": evidence_groups(context),
            "request_context": context.request_context,
            "snapshots": context.snapshots,
            "tool_traces": model_traces(context),
            "departments": context.departments,
            "observations": observations,
            "remaining_tool_calls": runtime.settings.max_tool_calls - gateway.calls,
        }
        step = await runtime.model.complete(NextStep, system, json_context(payload))
        if (
            step.action == "HOLD"
            and stage == "INVESTIGATING"
            and not hold_rejected
            and context.departments
            and usable_evidence(context, state)
        ):
            # Evidence and departments exist: unconfirmed field facts belong in the handover's
            # next checks, not in a hold. Asked once; a second HOLD is accepted.
            hold_rejected = True
            observations.append(
                {
                    "error": "HOLD_REJECTED",
                    "note": "조회한 근거와 부서 후보가 있다. FINISH로 조사안을 쓰고, "
                    "미확인 사항은 rationale의 '부서에 요청할 다음 확인'에 적는다. "
                    "근거로 부서를 정할 수 없을 때만 다시 HOLD한다.",
                }
            )
            continue
        if (
            step.action == "FINISH"
            and stage == "INVESTIGATING"
            and finish_rejections < 2
            and not usable_evidence(context, state)
        ):
            # An investigation without one retrieved case would cite invented evidence.
            finish_rejections += 1
            observations.append(
                {
                    "error": "FINISH_WITHOUT_EVIDENCE",
                    "note": "아직 조회한 근거가 없다. search_sdr_cases(영어 핵심어)나 "
                    "search_amm_references로 먼저 사례를 조회한다.",
                }
            )
            continue
        if step.action != "CALL":
            return step
        if step.tool_name in hidden:
            await gateway.call("TOOL_NOT_OFFERED", {}, step.question)
            observations.append(
                {
                    "tool": step.tool_name,
                    "error": "TOOL_NOT_OFFERED",
                    "note": "LLM1이 이 기체의 전체 이력을 이미 비교했고 연결할 이력이 없었다.",
                }
            )
            continue
        try:
            args = json.loads(step.arguments_json)
            if not isinstance(args, dict):
                raise ValueError("Arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError):
            # Count invalid choices too so a malformed planner cannot spin forever.
            await gateway.call("INVALID_ARGUMENT_JSON", {}, step.question)
            observations.append({"tool": step.tool_name, "error": "INVALID_ARGUMENT_JSON"})
            continue
        if "candidate_jasc" in args:
            # Only codes confirmed through search_jasc_codes; a guessed code fails the whole search.
            verified = {row.get("code") for row in context.jasc_candidates}
            kept = [c for c in args["candidate_jasc"] if c in verified]
            if kept != args["candidate_jasc"]:
                observations.append(
                    {
                        "tool": step.tool_name,
                        "note": "확인되지 않은 candidate_jasc를 제외했다. "
                        "search_jasc_codes로 확인한 코드만 쓴다.",
                        "dropped": [c for c in args["candidate_jasc"] if c not in verified],
                    }
                )
            args = {**args, "candidate_jasc": kept} if kept else {
                k: v for k, v in args.items() if k != "candidate_jasc"
            }
        identity = json.dumps([step.tool_name, args], sort_keys=True, ensure_ascii=False)
        if identity in seen_calls:
            # Counted like any call so a planner that keeps repeating itself still terminates.
            await gateway.call("DUPLICATE_CALL", {}, step.question)
            observations.append(
                {
                    "tool": step.tool_name,
                    "error": "DUPLICATE_CALL",
                    "note": "같은 인자로 이미 조회했다. evidence_records와 item_ids를 사용한다.",
                }
            )
            continue
        seen_calls.add(identity)
        result = await gateway.call(step.tool_name, args, step.question)
        returned = context.traces[-1].item_ids if context.traces else []
        observations.append(
            {
                "tool": step.tool_name,
                "question": step.question,
                "result": summarize_response(result),
                "item_ids": returned,
            }
        )
        if context.hold is not None:
            return NextStep(
                action="HOLD",
                question=context.hold.summary,
                tool_name=None,
                arguments_json=None,
                hold_reason="INSUFFICIENT_INFORMATION",
            )
    if usable_evidence(context, state) or (stage == "REVIEWING" and state["investigations"]):
        # Budget spent but the case has citable evidence: write the result instead of discarding it.
        return NextStep(
            action="FINISH",
            question="도구 예산을 모두 사용해 지금까지 조회한 근거로 작성한다.",
            tool_name=None,
            arguments_json=None,
            hold_reason=None,
        )
    return NextStep(
        action="HOLD",
        question="도구 호출 한도 안에 조사를 마치지 못했습니다.",
        tool_name=None,
        arguments_json=None,
        hold_reason="INSUFFICIENT_INFORMATION",
    )
