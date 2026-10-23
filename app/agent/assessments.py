"""Private, comparable views for LLM3. Never include them in LLM2 or public events."""

from app.agent.schemas import (
    AgentContext,
    Classification,
    GraphState,
    Investigation,
    ReviewAssessment,
)
from app.agent.tools import EvidenceValidationError, validate_department_ata


def review_inputs(state: GraphState, context: AgentContext) -> dict:
    classification = state.get("classification")
    classification = Classification.model_validate(classification) if classification else None
    latest = state.get("investigations", [])[-1] if state.get("investigations") else None
    investigation = Investigation.model_validate(latest) if latest else None
    records = {r.ref_id: r for r in context.records if r.source == "HISTORY"}
    evidence = []
    if classification:
        for item in context.classification_comparisons:
            record = records.get(item.get("history_id"))
            excerpt = item.get("history_excerpt", "")
            if (
                record
                and item.get("accepted")
                and item.get("source_quotes_verified")
                and record.ref_id in classification.linked_history_ids
                and excerpt
                and excerpt in record.content
            ):
                evidence.append(
                    {
                        "source": "HISTORY",
                        "ref_id": record.ref_id,
                        "excerpt": excerpt,
                        "why": item["rationale"],
                    }
                )
    llm1 = (
        None
        if classification is None
        else {
            "ata_chapter": classification.ata_chapter,
            "proposed_department": classification.proposed_department,
            "rationale": classification.rationale,
            "evidence": evidence,
            "sufficient": classification.sufficient,
            "evidence_status": "REFERENCES_VERIFIED" if evidence else "NO_VERIFIED_REFERENCES",
        }
    )
    llm2 = (
        None
        if investigation is None
        else {
            "ata_chapter": investigation.ata_chapter,
            "proposed_department": investigation.proposed_department,
            "rationale": investigation.rationale,
            "evidence": [e.model_dump(mode="json") for e in investigation.evidence],
            "sufficient": None,
            "evidence_status": "REFERENCES_VERIFIED"
            if investigation.evidence
            else "NO_VERIFIED_REFERENCES",
        }
    )
    differences = []
    if llm1 and llm2:
        for field in ("ata_chapter", "proposed_department"):
            if llm1[field] is not None and llm1[field] != llm2[field]:
                differences.append({"field": field, "llm1": llm1[field], "llm2": llm2[field]})
    return {
        "llm1": llm1,
        "llm2": llm2,
        "differences": differences,
        "llm1_context_comparisons": context.classification_comparisons,
        "instruction": "두 판단은 가설이다. 동의 여부보다 원문 근거를 우선하고, "
        "LLM1의 유보·차이와 LLM2의 보강 근거를 대조한다.",
    }


def guard_review(assessment: ReviewAssessment, state: GraphState, context: AgentContext):
    """Prevent PASS with an unaddressed conflict or unsupported current department.

    Different answers may pass when the model explains a resolution using final evidence.
    This checks declared results/provenance, not the truth of model semantic judgments.
    """
    output, comparison = assessment.review, assessment.comparison
    if output.decision != "PASS":
        return output
    materials = review_inputs(state, context)
    issue = None
    if comparison.status == "UNRESOLVED_DIFFERENCE":
        issue = "판단 간 핵심 충돌이 미해결이다. 원문 근거로 조사안을 보완하세요."
    elif (comparison.status == "NOT_AVAILABLE") != (materials["llm1"] is None):
        issue = "제공된 LLM1 판단의 유무와 검토 결과가 다릅니다. 비교 자료를 다시 확인하세요."
    elif materials["differences"] and comparison.status != "RESOLVED_DIFFERENCE":
        issue = "ATA·부서 판단의 차이를 원문 근거로 해소하고 조사안을 보완하세요."
    latest = Investigation.model_validate(state["investigations"][-1])
    final_refs = {
        e.ref_id for e in [*latest.evidence, *assessment.additional_evidence] if e.source != "AMM"
    }
    if not set(comparison.evidence_refs) <= final_refs:
        raise EvidenceValidationError("비교에 사용한 근거는 최종 인용 근거에도 포함해야 합니다.")
    if materials["llm1"] and not comparison.evidence_refs:
        issue = "두 판단을 비교한 원문 근거를 최종 evidence에 포함해 확인하세요."
    try:
        validate_department_ata(output.department, latest.ata_chapter, context)
    except EvidenceValidationError as exc:
        issue = f"{exc} 증상 ATA와 담당 부서의 관계를 재조사하세요."
    if issue:
        return output.model_copy(
            update={
                "decision": "SUPPLEMENT",
                "department": None,
                "comments": output.comments + " [실행기: " + issue + "]",
                "supplement_request": issue,
            }
        )
    return output
