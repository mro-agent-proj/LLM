import copy
import json
from uuid import uuid4

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.runtime import AgentRuntime
from app.agent.workflow import build_graph
from app.messages import RunRequest
from app.server.journal import RunJournal
from app.server.rabbit import handle_message
from app.server.runner import EventPublishError
from app.service import EventDeliveryError, MaintenanceService, RunConflict
from tests.support import DemoTools, ScriptedDemoModel, example_state
from tests.test_ai_server import Exchange, Message, runtime


def request() -> RunRequest:
    state = example_state()
    return RunRequest(
        request_id=str(uuid4()),
        **{k: state[k] for k in ("tail_number", "aircraft_model", "airline_code", "symptom_text")},
    )


@pytest.mark.asyncio
async def test_worker_graph_publishes_real_stage_outputs_and_terminal_decision(tmp_path):
    run = request()
    received = []

    async def publish(item):
        received.append(copy.deepcopy(item))

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        result = await MaintenanceService(saver, graph_factory=build_graph).execute(
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
        await MaintenanceService(saver, graph_factory=build_graph).execute(
            run, AgentRuntime(ScriptedDemoModel(), DemoTools()), record
        )
    replay = []

    async def replay_record(item):
        replay.append(item)

    class NoCall:
        async def complete(self, *args):
            raise AssertionError("A completed investigation must not rerun")

    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        await MaintenanceService(saver, graph_factory=build_graph).execute(
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
            await MaintenanceService(saver, graph_factory=build_graph).execute(
                run, AgentRuntime(model, DemoTools()), disconnect_after_investigation
            )
    outputs = []

    async def publish(item):
        outputs.append(item)

    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        result = await MaintenanceService(saver, graph_factory=build_graph).execute(
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
        service = MaintenanceService(saver, graph_factory=build_graph)
        run = request()
        runtime = AgentRuntime(Broken(), DemoTools())
        await service.execute(run, runtime, publish)
        original = events[-1]
        assert original["type"] == "FAILED"
        assert original["payload"] == {"error": "AI_EXECUTION_FAILED"}
        await service.execute(run, runtime, publish)
        assert events[-1] == original
    assert "secret-token" not in json.dumps(events)


@pytest.mark.asyncio
async def test_saved_request_cannot_be_replaced_with_different_symptom(tmp_path):
    async def publish(item):
        pass

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.db")) as saver:
        run = request()
        service = MaintenanceService(saver, graph_factory=build_graph)
        runtime = AgentRuntime(ScriptedDemoModel(), DemoTools())
        await service.execute(run, runtime, publish)
        with pytest.raises(RunConflict):
            await service.execute(
                run.model_copy(update={"symptom_text": "changed"}), runtime, publish
            )


@pytest.mark.asyncio
async def test_message_ack_only_after_confirmed_terminal_event(tmp_path):
    message, exchange = Message(), Exchange()

    async def factory(run, headers):
        return runtime()

    await handle_message(
        message, exchange, factory, journal=RunJournal(str(tmp_path / "journal.sqlite"))
    )
    assert message.acked
    assert json.loads(exchange.messages[-1].body)["type"] == "COMPLETED"


@pytest.mark.asyncio
async def test_publish_disconnect_leaves_delivery_unacknowledged(tmp_path):
    message = Message()

    class Disconnected(Exchange):
        async def publish(self, *args, **kwargs):
            raise ConnectionError("disconnected")

    async def factory(run, headers):
        return runtime()

    with pytest.raises(EventPublishError):
        await handle_message(
            message,
            Disconnected(),
            factory,
            journal=RunJournal(str(tmp_path / "journal.sqlite")),
        )
    assert not message.acked and message.rejected is None


@pytest.mark.asyncio
async def test_invalid_message_rejected_without_running_model(tmp_path):
    message = Message()
    message.body = b'{"request_id":"invalid"}'
    await handle_message(
        message, Exchange(), None, journal=RunJournal(str(tmp_path / "journal.sqlite"))
    )
    assert not message.acked and message.rejected is False


@pytest.mark.asyncio
async def test_full_graph_resumes_after_classification_without_rerunning_llm1(tmp_path):
    from tests.llm1_fixtures import BusinessTools, FullModel

    run, model = request(), FullModel()
    original = []

    async def disconnect(item):
        original.append(copy.deepcopy(item))
        if item["type"] == "STAGE_RESULT":
            raise ConnectionError("synthetic disconnect")

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "full.sqlite")) as saver:
        service = MaintenanceService(saver)
        with pytest.raises(EventDeliveryError):
            await service.execute(run, AgentRuntime(model, BusinessTools()), disconnect)
        replay = []

        async def record(item):
            replay.append(copy.deepcopy(item))

        result = await service.execute(run, AgentRuntime(model, BusinessTools()), record)
    assert result["outcome"] == "ASSIGN"
    assert replay[:2] == original
    assert sum(schema == "ClassificationDecision" for schema, _ in model.calls) == 1
    assert not any(schema == "InvestigationAssessment" for schema, _ in model.calls)


@pytest.mark.asyncio
async def test_delegation_aliases_require_both_tokens_and_reject_conflicts(monkeypatch):
    from app.credentials import credentials_for

    monkeypatch.setenv("MCP_DELEGATION_TRANSPORT", "amqp_headers")
    credentials = await credentials_for(request(), {"rag_token": "r", "business_token": "b"})
    assert credentials.rag_token == "r" and credentials.business_token == "b"
    with pytest.raises(ValueError):
        await credentials_for(request(), {"rag_token": "r"})
    with pytest.raises(ValueError):
        await credentials_for(
            request(), {"rag_token": "r", "business_token": "b", "x-mro-rag-token": "different"}
        )
