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
    records = model_records(context, state)
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


def test_history_tool_is_hidden_only_when_llm1_found_nothing_to_link():
    from app.agent.nodes.common import hidden_tools

    read = AgentContext(request_id="r", records=[history(1)])
    assert hidden_tools(read, _investigation_state([]), "INVESTIGATING") == {"get_aircraft_history"}
    assert hidden_tools(read, _investigation_state(["MH-000001"]), "INVESTIGATING") == set()
    assert hidden_tools(read, _investigation_state([]), "REVIEWING") == set()
    unread = AgentContext(request_id="r")
    assert hidden_tools(unread, _investigation_state([]), "INVESTIGATING") == set()


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
