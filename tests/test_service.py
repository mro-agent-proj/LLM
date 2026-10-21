import copy
import json
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.runtime import AgentRuntime
from app.messages import RunRequest
from app.service import EventDeliveryError, MaintenanceService, RunConflict
from app.worker import handle_message
from tests.support import DemoTools, ScriptedDemoModel, example_state


def request() -> RunRequest:
    state = example_state()
    return RunRequest(
        request_id=uuid4(),
        **{k: state[k] for k in ("tail_number", "aircraft_model", "airline_code", "symptom_text")},
    )


@pytest.mark.asyncio
async def test_worker_graph_publishes_real_stage_outputs_and_terminal_decision(tmp_path):
    run = request()
    received = []

    async def publish(item):
        received.append(copy.deepcopy(item))

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        result = await MaintenanceService(saver).execute(
            run, AgentRuntime(ScriptedDemoModel(), DemoTools()), publish
        )
    assert result["outcome"] == "ASSIGN"
    assert [e["type"] for e in received] == [
        "STAGE_STARTED",
        "STAGE_RESULT",
        "STAGE_STARTED",
        "STAGE_RESULT",
        "COMPLETED",
    ]
    assert received[1]["payload"]["output"]["finding"]
    assert received[3]["payload"]["output"]["decision"] == "PASS"
    assert all(e["request_id"] == str(run.request_id) for e in received)


@pytest.mark.asyncio
async def test_redelivery_replays_stored_ids_without_rerunning_completed_model(tmp_path):
    run = request()
    first = []

    async def record(item):
        first.append(item)

    db = str(tmp_path / "state.db")
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        await MaintenanceService(saver).execute(
            run, AgentRuntime(ScriptedDemoModel(), DemoTools()), record
        )
    replay = []

    async def replay_record(item):
        replay.append(item)

    class NoCall:
        async def complete(self, *args):
            raise AssertionError("A completed investigation must not rerun")

    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        await MaintenanceService(saver).execute(
            run, AgentRuntime(NoCall(), DemoTools()), replay_record
        )
    assert replay == first


@pytest.mark.asyncio
async def test_delivery_failure_resumes_after_checkpoint_not_from_new_investigation(tmp_path):
    run = request()
    interrupted = []

    async def disconnect_after_investigation(item):
        interrupted.append(item)
        if item["type"] == "STAGE_RESULT":
            raise ConnectionError("synthetic broker disconnect")

    model = ScriptedDemoModel()
    db = str(tmp_path / "state.db")
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        with pytest.raises(EventDeliveryError):
            await MaintenanceService(saver).execute(
                run, AgentRuntime(model, DemoTools()), disconnect_after_investigation
            )
    outputs = []

    async def publish(item):
        outputs.append(item)

    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        result = await MaintenanceService(saver).execute(
            run, AgentRuntime(model, DemoTools()), publish
        )
    assert result["outcome"] == "ASSIGN"
    assert sum(schema == "InvestigationAssessment" for schema, _ in model.calls) == 1
    assert outputs[:2] == interrupted


@pytest.mark.asyncio
async def test_model_exception_emits_sanitized_failed_and_does_not_repeat(tmp_path):
    class Broken:
        async def complete(self, *args):
            raise RuntimeError("secret-token-must-not-leak")

    events = []

    async def publish(item):
        events.append(item)

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        service = MaintenanceService(saver)
        run = request()
        runtime = AgentRuntime(Broken(), DemoTools())
        await service.execute(run, runtime, publish)
        original = events[-1]
        assert original["type"] == "FAILED"
        assert original["payload"] == {"error": "EXECUTION_FAILED"}
        await service.execute(run, runtime, publish)
        assert events[-1] == original
    assert "secret-token" not in json.dumps(events)


@pytest.mark.asyncio
async def test_saved_request_cannot_be_replaced_with_different_symptom(tmp_path):
    async def publish(item):
        pass

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        run = request()
        service = MaintenanceService(saver)
        runtime = AgentRuntime(ScriptedDemoModel(), DemoTools())
        await service.execute(run, runtime, publish)
        with pytest.raises(RunConflict):
            await service.execute(
                run.model_copy(update={"symptom_text": "changed"}), runtime, publish
            )


class Message:
    def __init__(self, run):
        self.body = run.model_dump_json().encode()
        self.content_type = "application/json"
        self.message_id = str(run.request_id)
        self.headers = {}
        self.actions = []

    async def ack(self):
        self.actions.append("ACK")

    async def reject(self, requeue):
        assert requeue is False
        self.actions.append("REJECT")


@pytest.mark.asyncio
async def test_message_ack_only_after_confirmed_terminal_event(tmp_path):
    message = Message(request())

    class Service:
        async def execute(self, run, runtime, publish):
            message.actions.append("COMPLETED_CONFIRMED")

    await handle_message(message, Service(), ScriptedDemoModel(), None)
    assert message.actions == ["COMPLETED_CONFIRMED", "ACK"]


@pytest.mark.asyncio
async def test_publish_disconnect_leaves_delivery_unacknowledged():
    message = Message(request())

    class Service:
        async def execute(self, *args):
            raise EventDeliveryError("disconnected")

    with pytest.raises(EventDeliveryError):
        await handle_message(message, Service(), ScriptedDemoModel(), None)
    assert message.actions == []


@pytest.mark.asyncio
async def test_invalid_message_rejected_without_running_model():
    message = Message(request())
    message.body = b'{"request_id":"invalid"}'
    await handle_message(message, None, None, None)
    assert message.actions == ["REJECT"]
