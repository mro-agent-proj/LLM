import copy

import pytest

from app.agent.classification import ClassificationDecision, SymptomTriage
from app.agent.full_workflow import build_full_graph
from app.agent.nodes.classify import run_classify
from app.agent.runtime import AgentRuntime
from app.agent.settings import Settings
from tests.llm1_fixtures import BusinessTools, FullModel
from tests.support import example_state


@pytest.mark.asyncio
async def test_sufficient_uses_business_only_and_revalidates_before_assign():
    tools, model = BusinessTools(), FullModel()
    state = await build_full_graph(AgentRuntime(model, tools)).ainvoke(example_state())
    payload = state["agent_context"]["prepared_payload"]
    assert payload["outcome"] == "ASSIGN"
    assert payload["evidence"][0]["ref_id"] == "MH-000123"
    assert all(call == ("business",) for call in tools.server_calls)
    assert sum(name == "get_aircraft_history" for name, _ in tools.calls) == 2
    assert state["investigations"] == state["reviews"] == []


@pytest.mark.asyncio
async def test_insufficient_routes_to_existing_llm23_and_preserves_classification():
    tools, model = BusinessTools(), FullModel(False)
    state = await build_full_graph(AgentRuntime(model, tools)).ainvoke(example_state())
    assert not state["classification"].sufficient
    assert len(state["investigations"]) == len(state["reviews"]) == 1
    assert state["agent_context"]["prepared_payload"]["outcome"] == "ASSIGN"
    assert any(name == "get_evidence" for name, _ in tools.calls)


@pytest.mark.asyncio
async def test_full_graph_keeps_two_reinvestigations():
    state = await build_full_graph(
        AgentRuntime(FullModel(False, supplements=10), BusinessTools())
    ).ainvoke(example_state())
    assert state["retry_count"] == 2
    assert len(state["investigations"]) == 3
    assert state["agent_context"]["prepared_payload"]["review_reason"] == "RETRY_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"linked_history_ids": ["MH-999999"]},
        {"linked_history_ids": ["MH-000123", "MH-000123"]},
        {"proposed_department": "UNKNOWN"},
    ],
)
async def test_invalid_model_links_never_assign(changes):
    state = await build_full_graph(AgentRuntime(FullModel(**changes), BusinessTools())).ainvoke(
        example_state()
    )
    assert state["classification"] is None
    assert state["agent_context"]["prepared_payload"]["review_reason"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["other_aircraft", "missing_aircraft", "unlinked_department", "error"]
)
async def test_history_scope_or_failure_is_not_normal_insufficiency(mutation):
    tools, model = BusinessTools(), FullModel()
    if mutation == "other_aircraft":
        tools.history[0]["aircraft_id"] = "OTHER"
    elif mutation == "missing_aircraft":
        del tools.history[0]["aircraft_id"]
    elif mutation == "unlinked_department":
        tools.history[0]["department_id"] = "OTHER"
    else:
        tools.on_call = lambda name, args, result: (
            {"status": "ERROR", "error": {"code": "UPSTREAM_UNAVAILABLE", "retryable": False}}
            if name == "get_aircraft_history"
            else result
        )
    state = await build_full_graph(AgentRuntime(model, tools)).ainvoke(example_state())
    assert state["classification"] is None
    assert state["agent_context"]["prepared_payload"]["outcome"] == "HUMAN_REVIEW"


@pytest.mark.asyncio
async def test_no_history_is_a_valid_false_classification_not_a_tool_error():
    tools = BusinessTools()
    tools.history = []
    state = await run_classify(
        example_state(),
        AgentRuntime(FullModel(False, linked_history_ids=[], proposed_department=None), tools),
    )
    assert state["classification"].sufficient is False
    assert state["agent_context"]["hold"] is None


@pytest.mark.asyncio
async def test_pagination_reads_next_page_without_symptom_or_date_filter():
    tools, model = BusinessTools(), FullModel()

    def pages(name, args, response):
        if name == "get_aircraft_history" and "cursor" not in args:
            response["items"][0]["history_id"] = "MH-000001"
            response["next_cursor"] = "PAGE2"
        return response

    tools.on_call = pages
    result = await run_classify(example_state(), AgentRuntime(model, tools))
    assert result["classification"].linked_history_ids == ["MH-000123"]
    calls = [a for n, a in tools.calls if n == "get_aircraft_history"]
    assert len(calls) == 2 and calls[-1]["cursor"] == "PAGE2"
    assert all("query" not in a and "occurred_from" not in a for a in calls)


@pytest.mark.asyncio
async def test_incomplete_pages_do_not_become_a_false_classification():
    tools = BusinessTools()
    tools.on_call = lambda n, a, r: (
        {**r, "next_cursor": "NEXT"} if n == "get_aircraft_history" else r
    )
    result = await run_classify(
        example_state(), AgentRuntime(FullModel(), tools, Settings(max_tool_calls=3))
    )
    assert result["classification"] is None
    assert result["agent_context"]["hold"]["reason"] == "TOOL_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["removed", "inactive", "version", "department", "quote"])
async def test_direct_finalization_rejects_stale_evidence(change):
    tools = BusinessTools()

    def mutate(name, args, result):
        count = sum(n == name for n, _ in tools.calls)
        if name == "get_request_context" and count == 2 and change == "version":
            result["context_version"] = result["result"]["context_version"] = 2
        if name == "search_departments" and count == 2 and change == "inactive":
            result["items"][0]["active"] = False
        if name == "get_aircraft_history" and count == 2:
            if change == "removed":
                result.update(status="NO_MATCH", items=[])
            elif change == "department":
                result["items"][0]["department_id"] = "OTHER"
            elif change == "quote":
                result["items"][0]["symptom"] = "changed symptom"
        return result

    tools.on_call = mutate
    state = await build_full_graph(AgentRuntime(FullModel(), tools)).ainvoke(example_state())
    assert state["agent_context"]["prepared_payload"]["review_reason"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_new_department_fcs_and_uuid_directory_mapping_work_without_hardcoding():
    tools = BusinessTools()
    tools.departments[0].update(code="FCS", department_id="department-uuid", ata_chapters="27")
    tools.history[0].update(department_id="department-uuid", ata_chapter="27", jasc_code="2750")
    result = await run_classify(
        example_state(), AgentRuntime(FullModel(proposed_department="FCS", ata_chapter="27"), tools)
    )
    assert result["classification"].proposed_department == "FCS"


@pytest.mark.asyncio
async def test_unknown_ata_has_an_explicit_hold_without_invented_classification():
    class Unknown:
        async def complete(self, schema, system, context):
            if schema is SymptomTriage:
                return SymptomTriage(candidate_ata=["32"], rationale="휠 증상")
            return ClassificationDecision(
                classification=None, missing_information="증상 추가 설명 필요", link_evidence=[]
            )

    result = await run_classify(example_state(), AgentRuntime(Unknown(), BusinessTools()))
    assert result["classification"] is None
    assert result["agent_context"]["hold"]["reason"] == "INSUFFICIENT_INFORMATION"


@pytest.mark.asyncio
async def test_input_unchanged_and_llm_failures_propagate():
    class Broken:
        async def complete(self, *args):
            raise RuntimeError("provider failed")

    state = example_state()
    original = copy.deepcopy(state)
    with pytest.raises(RuntimeError):
        await run_classify(state, AgentRuntime(Broken(), BusinessTools()))
    assert state == original


@pytest.mark.asyncio
async def test_history_of_other_systems_is_never_shown_or_linked():
    """Symptom-only triage picks the system first; other systems' history cannot steer LLM1."""
    tools, model = BusinessTools(), FullModel()
    model.triage_ata = ["33"]  # e.g. an exit-sign light symptom; the only history is ATA 32
    result = await run_classify(example_state(), AgentRuntime(model, tools))
    output = result["classification"]
    assert output.sufficient is False and output.linked_history_ids == []
    assert output.ata_chapter == "33" and output.proposed_department is None
    # No classification call at all, so there is nothing to misuse.
    assert [name for name, _ in model.calls] == ["SymptomTriage"]


@pytest.mark.asyncio
async def test_classification_sees_only_candidate_ata_history():
    tools, model = BusinessTools(), FullModel()
    door = dict(
        tools.history[0],
        history_id="MH-000200",
        source_id="MH-000200",
        ata_chapter="52",
        jasc_code="5210",
        symptom="door bottle pressure low",
        department_id="HYD",
    )
    tools.history.append(door)
    result = await run_classify(example_state(), AgentRuntime(model, tools))
    assert result["classification"].sufficient is True
    shown = next(payload for name, payload in model.calls if name == "ClassificationDecision")
    assert [r["ref_id"] for r in shown["history"]] == ["MH-000123"]
    assert shown["symptom_ata_candidates"] == ["32"]


@pytest.mark.parametrize(
    "triage, changes, link_changes",
    [
        (["32"], {"linked_history_ids": []}, {}),
        (["52"], {"ata_chapter": "32"}, {}),
        (["32", "52"], {"linked_history_ids": ["MH-000200"]}, {}),
        (["32"], {}, {"target_relation": "UNKNOWN"}),
        (["32"], {}, {"history_excerpt": "브레이크"}),
    ],
)
@pytest.mark.asyncio
async def test_unverified_direct_assignment_goes_to_investigation(triage, changes, link_changes):
    """An unverified direct link is not a tool failure: LLM1 hands the case to LLM2 instead."""
    tools = BusinessTools()
    door = dict(
        tools.history[0],
        history_id="MH-000200",
        source_id="MH-000200",
        ata_chapter="52",
        jasc_code="5210",
        symptom="door bottle pressure low",
        department_id="HYD",
    )
    tools.history.append(door)
    model = FullModel(**changes)
    model.triage_ata, model.link_changes = triage, link_changes
    result = await run_classify(example_state(), AgentRuntime(model, tools))
    output = result["classification"]
    assert result["agent_context"]["hold"] is None
    assert output.sufficient is False and output.linked_history_ids == []
    assert output.ata_chapter in triage


@pytest.mark.asyncio
async def test_verified_link_keeps_the_direct_path_and_its_evidence():
    tools, model = BusinessTools(), FullModel()
    state = await build_full_graph(AgentRuntime(model, tools)).ainvoke(example_state())
    assert state["classification"].sufficient is True
    assert state["agent_context"]["prepared_payload"]["outcome"] == "ASSIGN"
    assert state["investigations"] == []


def test_history_ata_reads_business_mcp_observations():
    from app.agent.nodes.classify import history_ata
    from app.agent.schemas import RetrievedRecord

    def record(metadata):
        return RetrievedRecord(source="HISTORY", ref_id="MH-000001", content="x", metadata=metadata)

    assert history_ata(record({"observations": ["ata_chapter=27", "jasc_code=2750"]})) == "27"
    assert history_ata(record({"ata_chapter": "32"})) == "32"
    assert history_ata(record({"observations": ["after landing"]})) is None


@pytest.mark.asyncio
async def test_blank_link_reason_drops_the_link_instead_of_failing_the_run():
    tools, model = BusinessTools(), FullModel()
    model.link_changes = {"current_excerpt": ""}
    result = await run_classify(example_state(), AgentRuntime(model, tools))
    assert result["agent_context"]["hold"] is None
    assert result["classification"].sufficient is False
