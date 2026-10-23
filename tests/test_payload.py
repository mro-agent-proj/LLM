"""Model input stays bounded however much history LLM1 read (128k context overflowed before)."""

from app.agent.nodes.common import MAX_UNCITED_HISTORY, model_records, model_traces
from app.agent.schemas import AgentContext, Classification, RetrievedRecord, ToolTrace


def history(i):
    return RetrievedRecord(
        source="HISTORY",
        ref_id=f"MH-{i:06d}",
        content="x" * 5000,
        metadata={
            "occurred_at": f"2026-01-{i % 28 + 1:02d}T00:00:00Z",
            "ata_chapter": "25",
            "retrieval": {"applied_scope": {"big": "y" * 2000}},
        },
    )


def test_history_is_cited_plus_recent_and_records_are_compact():
    sdr = RetrievedRecord(
        source="SDR", ref_id="EV-SDR-1", content="wheel", metadata={"operator_code": "AALA"}
    )
    context = AgentContext(request_id="r", records=[history(i) for i in range(1, 101)] + [sdr])
    state = {
        "classification": Classification(
            ata_chapter="25",
            linked_history_ids=["MH-000001"],
            sufficient=False,
            proposed_department=None,
            rationale="r",
        ),
        "investigations": [],
    }
    records = model_records(context, state, "REVIEWING")
    ids = {r["ref_id"] for r in records}
    assert "MH-000001" in ids and "EV-SDR-1" in ids
    assert sum(r["source"] == "HISTORY" for r in records) <= MAX_UNCITED_HISTORY + 1
    assert all(len(r["content"]) <= 1500 and "retrieval" not in r for r in records)
    assert next(r for r in records if r["ref_id"] == "EV-SDR-1")["operator_code"] == "AALA"


def test_traces_keep_the_envelope_without_item_bodies():
    trace = ToolTrace(
        tool="get_aircraft_history",
        question="q",
        arguments={"limit": 100},
        outcome="OK",
        attempts=1,
        item_ids=["MH-000001"],
        response={
            "status": "OK",
            "counts": {"returned_count": 100},
            "items": [{"quote": "z" * 9000}],
        },
    )
    [compact] = model_traces(AgentContext(request_id="r", traces=[trace]))
    assert compact["response"] == {"status": "OK", "counts": {"returned_count": 100}}
    assert compact["item_ids"] == ["MH-000001"]


async def test_identical_repeat_call_is_not_executed_again():
    import json

    from app.agent.nodes.common import select_evidence
    from app.agent.runtime import AgentRuntime
    from app.agent.schemas import NextStep
    from app.agent.settings import Settings
    from app.agent.tools import ToolGateway
    from tests.support import DemoTools, example_state

    class Repeater:
        def __init__(self):
            self.seen = []

        async def complete(self, schema, system, context):
            self.seen.append(json.loads(context)["observations"])
            return NextStep(
                action="CALL",
                question="같은 조회를 반복한다.",
                tool_name="search_sdr_cases",
                arguments_json='{"query": "wheel vibration"}',
                hold_reason=None,
            )

    source, model = DemoTools(), Repeater()
    state = example_state()
    ctx = source.response("get_request_context", {"request_id": state["request_id"]})["result"]
    context = AgentContext(
        request_id=state["request_id"],
        context_version=1,
        request_context=ctx,
        applied_scope=dict(source.scope),
    )
    settings = Settings(max_tool_calls=4)
    gateway = ToolGateway(source.tools, state, context, settings)
    step = await select_evidence(
        state, context, gateway, AgentRuntime(model, source, settings), "s", "INVESTIGATING"
    )
    # The budget still ends the loop; the SDR evidence already found is written up, not discarded.
    assert step.action == "FINISH"
    assert sum(name == "search_sdr_cases" for name, _ in source.calls) == 1
    assert model.seen[1][0]["item_ids"] == ["EV-SDR-DEMO-001"]
    assert model.seen[2][-1]["error"] == "DUPLICATE_CALL"


def _investigation_state(linked):
    from app.agent.schemas import Classification

    return {
        "classification": Classification(
            ata_chapter="32",
            linked_history_ids=linked,
            sufficient=False,
            proposed_department=None,
            rationale="r",
        ),
        "investigations": [],
    }


def test_history_tool_is_available_regardless_of_llm1_links():
    from app.agent.nodes.common import hidden_tools

    # AMM tools are never offered: AMM is attached after assignment as a reference.
    amm = {"search_amm_references", "get_sdr_amm_references"}
    read = AgentContext(request_id="r", records=[history(1)])
    no_link = _investigation_state([])
    assert hidden_tools(read, no_link, "INVESTIGATING") == amm
    assert hidden_tools(read, _investigation_state(["MH-000001"]), "INVESTIGATING") == amm
    assert hidden_tools(read, _investigation_state([]), "REVIEWING") == amm
    unread = AgentContext(request_id="r")
    assert hidden_tools(unread, _investigation_state([]), "INVESTIGATING") == amm


async def test_hold_with_evidence_is_sent_back_once_then_accepted():
    import json

    from app.agent.nodes.common import select_evidence
    from app.agent.runtime import AgentRuntime
    from app.agent.schemas import NextStep
    from app.agent.settings import Settings
    from app.agent.tools import ToolGateway
    from tests.support import DemoTools, example_state

    class Holder:
        def __init__(self):
            self.seen = []

        async def complete(self, schema, system, context):
            self.seen.append(json.loads(context)["observations"])
            return NextStep(
                action="HOLD",
                question="현장 확인이 필요하다.",
                tool_name=None,
                arguments_json=None,
                hold_reason="INSUFFICIENT_INFORMATION",
            )

    source, model = DemoTools(), Holder()
    state = {**example_state(), **_investigation_state([])}
    sdr = RetrievedRecord(source="SDR", ref_id="EV-SDR-DEMO-001", content="wheel", metadata={})
    context = AgentContext(
        request_id=state["request_id"],
        context_version=1,
        records=[sdr],
        departments=[{"code": "HYD"}],
        applied_scope=dict(source.scope),
    )
    gateway = ToolGateway(source.tools, state, context, Settings())
    step = await select_evidence(
        state, context, gateway, AgentRuntime(model, source, Settings()), "s", "INVESTIGATING"
    )
    assert step.action == "HOLD"  # the second HOLD is respected
    assert model.seen[1][-1]["error"] == "HOLD_REJECTED"
    assert len(model.seen) == 2 and gateway.calls == 0


def test_llm1_links_do_not_change_llm2_history_selection():
    context = AgentContext(request_id="r", records=[history(i) for i in range(1, 30)])
    shown = model_records(context, _investigation_state([]))
    assert len([r for r in shown if r["source"] == "HISTORY"]) == MAX_UNCITED_HISTORY
    linked = model_records(context, _investigation_state(["MH-000001"]))
    assert shown == linked


def test_independently_selected_older_history_page_remains_visible():
    records = [history(i) for i in range(1, 30)]
    records[0].metadata["occurred_at"] = "2001-01-01"
    context = AgentContext(request_id="r", records=records)
    state = _investigation_state([])
    assert "MH-000001" not in {r["ref_id"] for r in model_records(context, state)}
    context.history_focus_ids = ["MH-000001"]
    assert "MH-000001" in {r["ref_id"] for r in model_records(context, state)}


async def test_finish_without_evidence_is_sent_back_twice_then_returned():
    import json

    from app.agent.nodes.common import select_evidence
    from app.agent.runtime import AgentRuntime
    from app.agent.schemas import NextStep
    from app.agent.settings import Settings
    from app.agent.tools import ToolGateway
    from tests.support import DemoTools, example_state

    class Finisher:
        def __init__(self):
            self.seen = []

        async def complete(self, schema, system, context):
            self.seen.append(json.loads(context)["observations"])
            return NextStep(
                action="FINISH",
                question="바로 작성",
                tool_name=None,
                arguments_json=None,
                hold_reason=None,
            )

    source, model = DemoTools(), Finisher()
    state = {**example_state(), **_investigation_state([])}
    context = AgentContext(
        request_id=state["request_id"],
        context_version=1,
        records=[],
        departments=[{"code": "HYD"}],
        applied_scope=dict(source.scope),
    )
    gateway = ToolGateway(source.tools, state, context, Settings())
    step = await select_evidence(
        state, context, gateway, AgentRuntime(model, source, Settings()), "s", "INVESTIGATING"
    )
    assert step.action == "FINISH" and len(model.seen) == 3
    assert model.seen[1][-1]["error"] == model.seen[2][-1]["error"] == "FINISH_WITHOUT_EVIDENCE"


def test_paraphrased_excerpt_is_replaced_with_original_text_unknown_id_is_not():
    from app.agent.schemas import Evidence
    from app.agent.tools import ground_evidence, validate_evidence

    sdr = RetrievedRecord(
        source="SDR",
        ref_id="EV-SDR-1",
        content="BRAKE VIBRATION ON TAXI. REPLACED BRAKE.",
        metadata={},
    )
    context = AgentContext(request_id="r", records=[sdr])
    good = Evidence(
        source="SDR", ref_id="EV-SDR-1", excerpt="지상 이동 중 브레이크 진동", why="유사 사례"
    )
    unknown = Evidence(source="SDR", ref_id="EV-SDR-9", excerpt="x", why="y")
    [fixed] = ground_evidence([good], context)
    assert fixed.excerpt == "BRAKE VIBRATION ON TAXI. REPLACED BRAKE."
    validate_evidence([fixed], context)
    import pytest

    with pytest.raises(Exception):
        validate_evidence(ground_evidence([unknown], context), context)


def test_citable_history_is_independent_of_llm1_links():
    from app.agent.nodes.common import citable

    row = history(1)
    rejected = _investigation_state([])
    linked = _investigation_state(["MH-000001"])
    assert citable(row, rejected, set()) is True
    assert citable(row, linked, {"MH-000001"}) is True
    assert citable(row, {"classification": None, "investigations": []}, set()) is True
