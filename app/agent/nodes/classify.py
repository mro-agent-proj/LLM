"""LLM1: read business MCP, classify, then validate the model's references."""

import re
from collections import Counter

from pydantic import ValidationError

from app.agent.classification import (
    CLASSIFY_SYSTEM,
    TRIAGE_SYSTEM,
    ClassificationDecision,
    LinkEvidence,
    SymptomTriage,
)
from app.agent.mcp_client import BUSINESS_TOOLS
from app.agent.nodes.common import hold_update, json_context
from app.agent.runtime import AgentRuntime, get_runtime
from app.agent.schemas import (
    AgentContext,
    Classification,
    Evidence,
    GraphState,
    RetrievedRecord,
)
from app.agent.tools import (
    EvidenceValidationError,
    ToolGateway,
    validate_department,
    validate_department_ata,
)


def history_ata(record: RetrievedRecord) -> str | None:
    """History ATA: a contract field, or "ata_chapter=NN" in business-mcp observations."""
    value = record.metadata.get("ata_chapter")
    if isinstance(value, str) and re.fullmatch(r"[0-9]{2}", value):
        return value
    for item in record.metadata.get("observations") or []:
        match = re.fullmatch(r"ata_chapter=([0-9]{2})", str(item))
        if match:
            return match.group(1)
    return None


def history_symptom(record: RetrievedRecord) -> str:
    """Do not turn text found only in actions/outcome into a historical symptom."""
    return record.metadata.get("symptom") or record.metadata.get("symptom_text") or ""


def verified_quotes(reason: LinkEvidence, record: RetrievedRecord, symptom: str) -> bool:
    return all(
        isinstance(quote, str) and 2 <= len(quote.strip()) <= 300 and quote in original
        for quote, original in (
            (reason.current_excerpt, symptom),
            (reason.history_excerpt, history_symptom(record)),
        )
    )


def supported_links(
    output: Classification, link_evidence: list[LinkEvidence], context: AgentContext, symptom: str
) -> list[str]:
    """Validate source quotes and require an explicit compatible semantic assessment.

    Meaning/polarity is evaluated by the LLM; code verifies provenance and rejects an
    assessment that itself reports a mismatch, denial or unresolved material conflict.
    """
    history = {r.ref_id: r for r in context.records if r.source == "HISTORY"}
    reasons = {e.history_id: e for e in link_evidence}
    counts = Counter(e.history_id for e in link_evidence)
    kept = []
    for identifier in output.linked_history_ids:
        record, reason = history.get(identifier), reasons.get(identifier)
        if (
            record is None
            or reason is None
            or counts[identifier] != 1
            or history_ata(record) != output.ata_chapter
        ):
            continue
        if not verified_quotes(reason, record, symptom):
            continue
        if not all(
            value.strip()
            for value in (
                reason.current_target,
                reason.history_target,
                reason.current_phenomenon,
                reason.history_phenomenon,
                reason.rationale,
            )
        ):
            continue
        if (
            reason.target_relation not in {"SAME", "RELATED"}
            or reason.phenomenon_relation not in {"SAME", "RELATED"}
            or reason.current_assertion != "PRESENT"
            or reason.history_assertion != "PRESENT"
            or reason.material_conflicts
        ):
            continue
        kept.append(identifier)
    return kept


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
        validate_department_ata(output.proposed_department, output.ata_chapter, context)
        if not ids:
            raise EvidenceValidationError("충분한 분류에는 동일 기체 연결 이력이 필요합니다.")
        try:
            comparisons = [
                LinkEvidence.model_validate(
                    {k: v for k, v in item.items() if k in LinkEvidence.model_fields}
                )
                for item in context.classification_comparisons
            ]
        except ValidationError as exc:
            raise EvidenceValidationError("저장된 맥락 비교 형식을 확인할 수 없습니다.") from exc
        symptom = (context.request_context.get("report") or {}).get("symptom", "")
        if set(supported_links(output, comparisons, context, symptom)) != set(ids):
            raise EvidenceValidationError("현재 원문과 저장된 맥락 비교를 확인하지 못했습니다.")
        # A direct assignment links history of the classified system only, never of another ATA.
        if any(history_ata(history[identifier]) != output.ata_chapter for identifier in ids):
            raise EvidenceValidationError("연결 이력의 ATA가 분류한 ATA와 다릅니다.")
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

    # Step 1: ATA hypotheses from the symptom alone, so the history cannot steer the system choice.
    triage = await runtime.model.complete(
        SymptomTriage,
        TRIAGE_SYSTEM,
        json_context(
            {
                "symptom_text": state["symptom_text"],
                "aircraft_model": state["aircraft_model"],
                "departments": context.departments,
            }
        ),
    )
    candidates = list(dict.fromkeys(triage.candidate_ata))
    # Step 2: only history of those systems is compared; other systems' rows are never candidates.
    relevant = [
        r for r in context.records if r.source == "HISTORY" and history_ata(r) in candidates
    ]
    if not relevant:
        output = Classification(
            ata_chapter=candidates[0],
            linked_history_ids=[],
            sufficient=False,
            proposed_department=None,
            rationale=(
                f"증상 기준 ATA 후보 {', '.join(candidates)}에 해당하는 동일 기체 이력이 없어 "
                f"조사 단계로 넘긴다. {triage.rationale}"
            ),
        )
        return {"classification": output, "agent_context": context.model_dump(mode="json")}
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
                "symptom_ata_candidates": candidates,
                "symptom_ata_rationale": triage.rationale,
                "history": [
                    {
                        "ref_id": r.ref_id,
                        "symptom": history_symptom(r),
                        "ata_chapter": history_ata(r),
                        **{
                            k: r.metadata.get(k)
                            for k in (
                                "department_id",
                                "department_code",
                                "occurred_at",
                                "reported_at",
                                "actions",
                                "action_taken",
                                "outcome",
                                "closed_at",
                            )
                        },
                    }
                    for r in sorted(
                        relevant,
                        key=lambda r: str(
                            r.metadata.get("occurred_at") or r.metadata.get("reported_at") or ""
                        ),
                        reverse=True,
                    )
                ],
                "departments": context.departments,
            }
        ),
    )
    if decision.classification is None:
        return stop("INSUFFICIENT_INFORMATION", decision.missing_information)
    output = decision.classification
    shown = {r.ref_id for r in relevant}
    ids = output.linked_history_ids
    if len(ids) != len(set(ids)) or not set(ids) <= {r.ref_id for r in context.records}:
        return stop("VALIDATION_FAILED", "조회되지 않았거나 중복된 이력이 연결됐습니다.")
    # The comparison explains semantic relevance; exact source quotes prove provenance only.
    kept = [
        i
        for i in supported_links(output, decision.link_evidence, context, state["symptom_text"])
        if i in shown
    ]
    direct = output.sufficient and output.ata_chapter in candidates and bool(kept)
    if direct:
        try:
            validate_department_ata(output.proposed_department, output.ata_chapter, context)
        except EvidenceValidationError:
            # A known but unsupported department needs investigation, not an invented mapping.
            if output.proposed_department not in {d["code"] for d in context.departments}:
                return stop("VALIDATION_FAILED", "조회되지 않은 부서를 제안했습니다.")
            direct = False
    records = {r.ref_id: r for r in relevant}
    context.classification_comparisons = [
        {
            **item.model_dump(mode="json"),
            "accepted": item.history_id in kept,
            "source_quotes_verified": item.history_id in records
            and verified_quotes(item, records[item.history_id], state["symptom_text"]),
        }
        for item in decision.link_evidence
    ]
    if output.sufficient and not direct:
        # An unverified direct assignment is not a tool failure: hand the case to LLM2 instead.
        output = output.model_copy(
            update={
                "ata_chapter": output.ata_chapter
                if output.ata_chapter in candidates
                else candidates[0],
                "linked_history_ids": kept,
                "sufficient": False,
                "rationale": output.rationale
                + " [실행기 확인: 맥락·원문·부서 담당 범위가 충분하지 않아 조사로 넘김]",
            }
        )
    else:
        output = output.model_copy(update={"linked_history_ids": kept})
    try:
        validate_classification(output, context)
    except EvidenceValidationError as exc:
        return stop("VALIDATION_FAILED", str(exc))
    if output.sufficient:
        context.final_evidence = [
            Evidence(
                source="HISTORY",
                ref_id=identifier,
                excerpt=next(
                    e.history_excerpt for e in decision.link_evidence if e.history_id == identifier
                ),
                why=output.rationale,
            )
            for identifier in output.linked_history_ids
        ]
    return {"classification": output, "agent_context": context.model_dump(mode="json")}


async def classify(state: GraphState) -> dict:
    return await run_classify(state, get_runtime())
