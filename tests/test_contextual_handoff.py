"""Fault injection for source grounding, independent investigation and review routing.

These scripted model tests exercise contracts/guards, not real-model semantic accuracy.
"""

import json

import pytest

from app.agent.assessments import guard_review, review_inputs
from app.agent.full_workflow import build_full_graph, finalize_classification
from app.agent.nodes.classify import run_classify
from app.agent.nodes.investigate import run_investigate
from app.agent.runtime import AgentRuntime
from app.agent.schemas import (
    AgentContext,
    Classification,
    Evidence,
    Investigation,
    JudgmentComparison,
    Review,
    ReviewAssessment,
)
from app.agent.tools import EvidenceValidationError, validate_department_ata
from tests.llm1_fixtures import BusinessTools, FullModel
from tests.support import example_state


def case(symptom, history, **link_changes):
    tools, model, state = BusinessTools(), FullModel(), example_state()
    state["symptom_text"] = symptom
    tools.history[0]["symptom"] = history
    model.link_changes = {
        "current_excerpt": symptom,
        "history_excerpt": history,
        **link_changes,
    }

    def sync_request(name, args, result):
        if name == "get_request_context":
            result["result"]["report"]["symptom"] = symptom
        return result

    tools.on_call = sync_request
    return state, model, tools


@pytest.mark.parametrize(
    "changes",
    [
        {"current_assertion": "ABSENT"},
        {"history_assertion": "ABSENT"},
        {"phenomenon_relation": "DIFFERENT"},
        {"target_relation": "UNKNOWN"},
        {"material_conflicts": ["현재의 정상 확인과 과거 결함이 상충한다."]},
        {"history_excerpt": "원문에 없는 인용"},
    ],
)
async def test_model_true_cannot_override_negative_or_unverified_comparison(changes):
    state, model, tools = case(
        "브레이크 이상 없음. 착륙장치 접힘 불가.", "브레이크 과열", **changes
    )
    result = await run_classify(state, AgentRuntime(model, tools))
    assert result["classification"].sufficient is False
    assert result["classification"].linked_history_ids == []
    assert not result["agent_context"]["hold"]


async def test_translated_equivalent_and_missing_conditions_can_keep_direct_route():
    state, model, tools = case(
        "제동장치 과열",
        "Brake overheating after landing",
        current_target="제동장치",
        history_target="Brake",
        current_phenomenon="과열",
        history_phenomenon="overheating",
        uncertainties=["현재 발생 단계 미기재"],
    )
    result = await build_full_graph(AgentRuntime(model, tools)).ainvoke(state)
    assert result["classification"].sufficient
    assert result["agent_context"]["prepared_payload"]["outcome"] == "ASSIGN"
    assert result["agent_context"]["final_evidence"][0]["excerpt"] == tools.history[0]["symptom"]
    assert not result["investigations"] and not result["reviews"]
    assert len(model.calls) == 2  # no extra semantic verifier API call


async def test_action_only_text_cannot_become_a_historical_symptom():
    state, model, tools = case(
        "브레이크 과열", "착륙장치 접힘 불가", history_excerpt="브레이크 정상"
    )
    tools.history[0]["actions"] = ["추가 점검: 브레이크 정상"]
    result = await run_classify(state, AgentRuntime(model, tools))
    assert result["classification"].sufficient is False
    assert not result["agent_context"]["classification_comparisons"][0]["source_quotes_verified"]


async def test_current_department_ata_mismatch_goes_to_independent_investigation():
    tools = BusinessTools()
    tools.departments[0]["ata_chapters"] = "29"
    result = await run_classify(example_state(), AgentRuntime(FullModel(), tools))
    assert result["classification"].sufficient is False
    assert not result["agent_context"]["hold"]


def test_department_responsibility_uses_public_b03_contract():
    context = AgentContext(
        request_id="r",
        departments=[
            {"code": "HYD", "responsibilities": ["ATA 29", "ATA 32"]},
        ],
    )
    validate_department_ata("HYD", "32", context)
    with pytest.raises(EvidenceValidationError):
        validate_department_ata("HYD", "27", context)


async def test_llm2_never_receives_llm1_judgment_but_llm3_does():
    model, tools = FullModel(False, rationale="LLM1_ONLY_MARKER"), BusinessTools()
    state = await build_full_graph(AgentRuntime(model, tools)).ainvoke(example_state())
    assert state["agent_context"]["prepared_payload"]["outcome"] == "ASSIGN"
    for schema, payload in model.calls:
        if schema == "InvestigationAssessment" or (
            schema == "NextStep" and payload["stage"] == "INVESTIGATING"
        ):
            assert "LLM1_ONLY_MARKER" not in json.dumps(payload)
            assert "classification" not in payload and "assessments" not in payload
        elif schema == "ReviewAssessment" or (
            schema == "NextStep" and payload["stage"] == "REVIEWING"
        ):
            assert payload["assessments"]["llm1"]["rationale"] == "LLM1_ONLY_MARKER"
    assert state["agent_context"]["review_comparisons"][0]["status"] == "CONSISTENT"


async def test_llm2_cannot_call_history_even_when_model_requests_it():
    from app.agent.schemas import InvestigationAssessment, NextStep
    from tests.support import ScriptedDemoModel

    class HistoryModel(ScriptedDemoModel):
        async def complete(self, schema, system, context):
            payload = json.loads(context)
            result = await super().complete(schema, system, context)
            if schema is NextStep and not payload["evidence_records"]:
                return result.model_copy(
                    update={
                        "action": "CALL",
                        "tool_name": "get_aircraft_history",
                        "arguments_json": "{}",
                    }
                )
            if schema is InvestigationAssessment:
                result.investigation.evidence = [
                    Evidence(
                        source="HISTORY",
                        ref_id="MH-000123",
                        excerpt="착륙 후 휠 진동",
                        why="직접 비교",
                    )
                ]
            return result

    state = example_state()
    state["classification"] = Classification(
        ata_chapter="52",
        linked_history_ids=[],
        sufficient=False,
        proposed_department="STR",
        rationale="LLM1_ONLY_MARKER",
    )
    tools = BusinessTools()
    result = await run_investigate(state, AgentRuntime(HistoryModel(), tools))
    assert result["agent_context"]["hold"] is not None
    assert not any(t["tool"] == "get_aircraft_history" for t in result["agent_context"]["traces"])


def review_case(status, classification=True):
    state = example_state()
    if classification:
        state["classification"] = Classification(
            ata_chapter="27",
            linked_history_ids=[],
            sufficient=False,
            proposed_department=None,
            rationale="초기 가설",
        )
    state["investigations"] = [
        Investigation(
            ata_chapter="32",
            finding="휠 증상",
            severity="MEDIUM",
            proposed_department="HYD",
            rationale="SDR로 보강",
            evidence=[
                Evidence(
                    source="SDR",
                    ref_id="EV-SDR-1",
                    excerpt="wheel vibration",
                    why="관련 현상",
                )
            ],
        )
    ]
    context = AgentContext(
        request_id=state["request_id"],
        departments=[
            {"code": "HYD", "responsibilities": ["ATA 32"]},
        ],
    )
    assessment = ReviewAssessment(
        review=Review(decision="PASS", department="HYD", comments="검토", supplement_request=None),
        comparison=JudgmentComparison(
            status=status, rationale="원문으로 비교", evidence_refs=["EV-SDR-1"]
        ),
        additional_evidence=[],
        technical_basis_ids=[],
    )
    return state, context, assessment


@pytest.mark.parametrize("status", ["CONSISTENT", "UNRESOLVED_DIFFERENCE", "NOT_AVAILABLE"])
def test_disagreement_cannot_pass_without_an_explicit_resolution(status):
    state, context, assessment = review_case(status)
    result = guard_review(assessment, state, context)
    assert result.decision == "SUPPLEMENT" and result.supplement_request


def test_resolved_difference_is_not_forced_to_llm1_and_null_department_is_preserved():
    state, context, assessment = review_case("RESOLVED_DIFFERENCE")
    assert guard_review(assessment, state, context).decision == "PASS"
    assert review_inputs(state, context)["llm1"]["proposed_department"] is None
    assert set(review_inputs(state, context)["llm1"]) == set(review_inputs(state, context)["llm2"])


def test_comparison_cannot_claim_unrecorded_final_evidence():
    state, context, assessment = review_case("RESOLVED_DIFFERENCE")
    assessment.comparison.evidence_refs = ["EV-SDR-UNKNOWN"]
    with pytest.raises(EvidenceValidationError):
        guard_review(assessment, state, context)


def test_llm3_without_llm1_is_still_supported():
    state, context, assessment = review_case("NOT_AVAILABLE", classification=False)
    assert guard_review(assessment, state, context).decision == "PASS"


async def test_legacy_direct_checkpoint_without_contextual_comparison_cannot_assign():
    runtime = AgentRuntime(FullModel(), BusinessTools())
    state = example_state()
    state.update(await run_classify(state, runtime))
    state["agent_context"]["classification_comparisons"] = []
    result = await finalize_classification(state, runtime)
    assert result["agent_context"]["prepared_payload"]["outcome"] == "HUMAN_REVIEW"


async def test_current_b03_scope_is_checked_again_before_direct_assignment():
    tools = BusinessTools()
    runtime = AgentRuntime(FullModel(), tools)
    state = example_state()
    state.update(await run_classify(state, runtime))
    tools.departments[0]["ata_chapters"] = "29"
    result = await finalize_classification(state, runtime)
    assert result["agent_context"]["prepared_payload"]["review_reason"] == "VALIDATION_FAILED"


async def test_b02_unavailable_to_investigator_gateway_but_available_to_reviewer():
    from app.agent.nodes.common import prepare

    runtime = AgentRuntime(FullModel(False), BusinessTools())
    for stage, allowed in [("INVESTIGATING", False), ("REVIEWING", True)]:
        context, gateway = await prepare(example_state(), runtime, stage=stage)
        assert ("get_aircraft_history" in {t["name"] for t in gateway.catalogue()}) is allowed
        result = await gateway.call("get_aircraft_history", {}, "역할별 B02 권한 검사")
        if allowed:
            assert result["status"] == "OK"
        else:
            assert result["error"]["code"] == "TOOL_NOT_AVAILABLE"
            assert context.traces[-1].attempts == 0
