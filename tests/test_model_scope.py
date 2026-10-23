"""Synthetic policy and harness regressions. No live model accuracy claims."""

import copy

import pytest

from app.agent.assessments import guard_review
from app.agent.model_scope import validate_policy
from app.agent.schemas import CrossModelReview, RetrievedRecord
from app.agent.tools import ToolResultError
from tests.support import DemoTools
from tests.test_contextual_handoff import review_case
from tests.test_tools import gateway

POLICY = {
    "policy_version": "airbus-model-scope-v1",
    "default_level": "MODEL_GENERATION",
    "levels": {
        "EXACT": ["A320214"],
        "MODEL_GENERATION": ["A320214", "A320232"],
        "FAMILY": ["A320214", "A320232", "A321211"],
    },
}


def expanded_tools():
    source = DemoTools()
    source.scope.update(model_scope=copy.deepcopy(POLICY), search_level="MODEL_GENERATION")
    # This test fixture advertises the new R02 contract; production obtains it from tools/list.
    for tool in source.tools:
        if tool.name == "search_sdr_cases":
            tool.args_schema = copy.deepcopy(tool.args_schema)
            tool.args_schema["properties"]["scope_level"] = {
                "type": "string",
                "enum": ["EXACT", "MODEL_GENERATION", "FAMILY"],
            }
    source.rows[0].update(
        model_key="A320232",
        requested_model_key="A320214",
        model_relation="MODEL_GENERATION",
        configuration_match="UNVERIFIED",
    )

    def result(name, args, data):
        if name == "search_sdr_cases":
            data["applied_scope"]["search_level"] = args["scope_level"]
        if name == "get_evidence":
            data["applied_scope"]["search_level"] = "FAMILY"
            data["result"].update(
                {
                    k: source.rows[0][k]
                    for k in (
                        "model_key",
                        "requested_model_key",
                        "model_relation",
                        "configuration_match",
                    )
                }
            )
        return data

    source.on_call = result
    return source


async def test_default_first_then_family_and_revalidate_metadata():
    source = expanded_tools()
    runner, context = gateway(source)
    with pytest.raises(ToolResultError):
        runner.scoped_arguments("search_sdr_cases", {"query": "wheel", "scope_level": "FAMILY"})
    found = await runner.call("search_sdr_cases", {"query": "wheel"}, "초기 조사")
    assert found["status"] == "OK", found
    assert source.calls[-1][1]["scope_level"] == "MODEL_GENERATION"
    assert (
        runner.scoped_arguments("search_sdr_cases", {"query": "wheel", "scope_level": "FAMILY"})[
            "scope_level"
        ]
        == "FAMILY"
    )
    verified = await runner.call(
        "get_evidence", {"evidence_id": source.rows[0]["evidence_id"]}, "원문 확인"
    )
    assert verified["status"] == "OK", verified
    source.rows[0]["model_key"] = "A321211"
    source.rows[0]["model_relation"] = "FAMILY"
    changed = await runner.call(
        "get_evidence", {"evidence_id": source.rows[0]["evidence_id"]}, "변조 확인"
    )
    assert changed["error"]["code"] == "EVIDENCE_CHANGED"


async def test_unapproved_model_is_rejected_even_when_result_claims_same_family():
    source = expanded_tools()
    source.rows[0].update(model_key="A330343", model_relation="FAMILY")
    runner, context = gateway(source)
    bad = await runner.call("search_sdr_cases", {"query": "wheel"}, "조사")
    assert bad["error"]["code"] == "SCOPE_VIOLATION"
    assert not context.records


@pytest.mark.parametrize("assessment", [None, "UNRESOLVED", "NOT_MATERIAL_FOR_DEPARTMENT"])
def test_llm3_requires_explicit_cross_model_applicability_review(assessment):
    state, context, result = review_case("RESOLVED_DIFFERENCE")
    context.applied_scope = {"model_key": "A320214", "model_scope": POLICY}
    context.records = [
        RetrievedRecord(
            source="SDR",
            ref_id="EV-SDR-1",
            content="wheel vibration",
            metadata={"model_key": "A320232", "configuration_match": "UNVERIFIED"},
        )
    ]
    if assessment:
        result.cross_model_reviews = [
            CrossModelReview(
                evidence_id="EV-SDR-1",
                same_target=True,
                same_phenomenon=True,
                conditions_compatible=True,
                configuration_assessment=assessment,
                rationale="휠 진동의 부서 비교이며 구성 차이가 부서 선택에 영향을 주지 않음",
            )
        ]
    assert guard_review(result, state, context).decision == (
        "PASS" if assessment == "NOT_MATERIAL_FOR_DEPARTMENT" else "SUPPLEMENT"
    )


def test_backend_allowance_is_validated_not_guessed():
    validate_policy({"model_key": "A320214", "model_scope": POLICY})
    invalid = copy.deepcopy(POLICY)
    invalid["levels"]["FAMILY"] = ["A321211"]
    with pytest.raises(ValueError):
        validate_policy({"model_key": "A320214", "model_scope": invalid})


async def test_full_graph_revalidates_expansion_and_marks_handoff():
    from app.agent.runtime import AgentRuntime
    from app.agent.schemas import ReviewAssessment
    from app.agent.workflow import build_graph
    from tests.support import ScriptedDemoModel, example_state

    class ScopeReviewModel(ScriptedDemoModel):
        async def complete(self, schema, system, context):
            result = await super().complete(schema, system, context)
            if schema is ReviewAssessment:
                result.cross_model_reviews = [
                    CrossModelReview(
                        evidence_id="EV-SDR-DEMO-001",
                        same_target=True,
                        same_phenomenon=True,
                        conditions_compatible=True,
                        configuration_assessment="NOT_MATERIAL_FOR_DEPARTMENT",
                        rationale="합성 휠 진동의 동일 부서 사례로만 비교하며 구성 적용성은 미확인",
                    )
                ]
            return result

    source = expanded_tools()
    state = await build_graph(AgentRuntime(ScopeReviewModel(), source)).ainvoke(example_state())
    payload = state["agent_context"]["prepared_payload"]
    assert payload["outcome"] == "ASSIGN", payload
    assert "A320232" in payload["summary"] and "확인되지 않음" in payload["summary"]
    assert any(name == "get_evidence" for name, _ in source.calls)
