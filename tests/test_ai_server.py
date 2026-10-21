import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pamqp.commands import Basic

from app.agent.demo import example_state
from app.agent.runtime import AgentRuntime
from app.main import create_app
from app.server.contracts import RunRequest, make_event
from app.server.delegation import HeaderRuntimeFactory
from app.server.journal import RunJournal
from app.server.rabbit import WorkerState, handle_message, publish_confirmed
from app.server.runner import EventPublishError, execute_run
from tests.llm1_fixtures import BusinessTools, FullModel

REQUEST_ID = "a2222222-2222-4222-8222-222222222222"


def request():
    data = example_state()
    return RunRequest(
        **{k: REQUEST_ID if k == "request_id" else data[k] for k in RunRequest.model_fields}
    )


def runtime():
    return AgentRuntime(FullModel(), BusinessTools())


class Message:
    def __init__(self):
        self.body = request().model_dump_json().encode()
        self.content_type = "application/json"
        self.message_id = REQUEST_ID
        self.headers = {"secret": "never-include-this-token"}
        self.redelivered = False
        self.timestamp = None
        self.acked = False
        self.rejected = None

    async def ack(self):
        self.acked = True

    async def reject(self, requeue):
        self.rejected = requeue


class Exchange:
    def __init__(self):
        self.messages = []

    async def publish(self, message, **kwargs):
        assert kwargs["routing_key"] == "agent.event"
        assert kwargs["mandatory"] is True
        self.messages.append(message)
        return Basic.Ack()


@pytest.mark.asyncio
async def test_events_only_include_public_classification_and_final_evidence():
    events = []

    async def publish(event):
        events.append(event)

    await execute_run(request(), runtime(), publish)
    assert [e.type for e in events] == ["STAGE_STARTED", "STAGE_RESULT", "COMPLETED"]
    assert events[1].payload["stage"] == "CLASSIFYING"
    assert events[2].payload["outcome"] == "ASSIGN"
    assert "agent_context" not in json.dumps([e.model_dump() for e in events])


@pytest.mark.asyncio
async def test_runner_exceptions_and_timeouts_emit_failed_without_sensitive_details():
    class Broken:
        async def complete(self, *args):
            raise RuntimeError("private-secret-token")

    class Slow:
        async def complete(self, *args):
            await asyncio.sleep(1)

    for model, expected in [(Broken(), "AI_EXECUTION_FAILED"), (Slow(), "RUN_TIMEOUT")]:
        events = []

        async def publish(event):
            events.append(event)

        await execute_run(
            request(), AgentRuntime(model, BusinessTools()), publish, timeout_seconds=0.05
        )
        assert events[-1].type == "FAILED"
        assert events[-1].payload == {"error": expected}
        assert "private-secret-token" not in str(events)


@pytest.mark.asyncio
async def test_uncertain_terminal_publish_does_not_emit_contradictory_failure():
    events = []

    async def publish(event):
        events.append(event)
        if event.type == "COMPLETED":
            await asyncio.sleep(1)

    with pytest.raises(EventPublishError):
        await execute_run(request(), runtime(), publish, timeout_seconds=0.1)
    assert events[-1].type == "COMPLETED"
    assert not any(event.type == "FAILED" for event in events)


@pytest.mark.asyncio
async def test_publish_retry_uses_exact_same_event_id_and_body():
    class Flaky(Exchange):
        async def publish(self, message, **kwargs):
            self.messages.append(message)
            if len(self.messages) == 1:
                raise TimeoutError()
            return Basic.Ack()

    exchange = Flaky()
    event = make_event(REQUEST_ID, "FAILED", {"error": "synthetic"})
    await publish_confirmed(exchange, event)
    assert exchange.messages[0].body == exchange.messages[1].body
    assert exchange.messages[0].message_id == exchange.messages[1].message_id


@pytest.mark.asyncio
async def test_confirmed_terminal_is_durable_and_ack_loss_replays_without_model(tmp_path):
    exchange, message = Exchange(), Message()
    journal = RunJournal(str(tmp_path / "journal.sqlite"))
    calls = []

    async def factory(run, headers):
        calls.append(run)
        return runtime()

    await handle_message(message, exchange, factory, journal=journal)
    assert message.acked and message.rejected is None
    original = [m.body for m in exchange.messages]
    redelivered = Message()
    redelivered.redelivered = True
    restarted_journal = RunJournal(str(tmp_path / "journal.sqlite"))
    await handle_message(redelivered, exchange, factory, journal=restarted_journal)
    assert redelivered.acked and len(calls) == 1
    assert [m.body for m in exchange.messages[len(original) :]] == original
    assert "never-include-this-token" not in (tmp_path / "journal.sqlite").read_bytes().decode(
        errors="ignore"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["invalid", "missing_delegation", "publish_nack", "changed_input"])
async def test_permanent_failure_rejects_without_ack_or_requeue(kind, tmp_path):
    message, exchange = Message(), Exchange()
    journal = RunJournal(str(tmp_path / "journal.sqlite"))
    if kind == "invalid":
        message.body = b"{}"
    if kind == "changed_input":
        journal.begin(REQUEST_ID, b"different original message")
    if kind == "publish_nack":

        async def nack(*args, **kwargs):
            return Basic.Nack()

        exchange.publish = nack

    async def factory(run, headers):
        if kind == "missing_delegation":
            raise ValueError("missing delegation")
        return runtime()

    await handle_message(message, exchange, factory, journal=journal)
    assert not message.acked and message.rejected is False


@pytest.mark.asyncio
async def test_shutdown_cancellation_does_not_ack_or_reject_inflight(tmp_path):
    message = Message()

    async def factory(run, headers):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await handle_message(
            message, Exchange(), factory, journal=RunJournal(str(tmp_path / "journal.sqlite"))
        )
    assert not message.acked and message.rejected is None


@pytest.mark.asyncio
async def test_request_delegations_create_separate_clients_and_never_enter_state(monkeypatch):
    captured = []

    def provider(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr("app.server.delegation.MCPToolProvider", provider)
    factory = HeaderRuntimeFactory(runtime().model, runtime().settings)
    first = await factory(request(), {"x-mro-business-token": "b1", "x-mro-rag-token": "r1"})
    second = await factory(request(), {"x-mro-business-token": "b2", "x-mro-rag-token": "r2"})
    assert first.tools is not second.tools
    assert captured == [
        {"business_token": "b1", "rag_token": "r1"},
        {"business_token": "b2", "rag_token": "r2"},
    ]
    with pytest.raises(ValueError):
        await factory(request(), {})


def test_fastapi_health_is_not_an_http_execution_api(monkeypatch):
    monkeypatch.setenv("AI_MODE", "disabled")
    with TestClient(create_app()) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        assert client.post("/run", json=request().model_dump()).status_code == 404


def test_fastapi_starts_and_stops_injected_worker():
    calls = []

    class Worker:
        state = WorkerState()

        async def start(self):
            calls.append("start")
            self.state = SimpleNamespace(connected=True, busy=False, reason="READY")

        async def stop(self):
            calls.append("stop")

    with TestClient(create_app(worker_factory=Worker)) as client:
        assert client.get("/health/ready").status_code == 200
    assert calls == ["start", "stop"]


def test_unagreed_auth_carrier_does_not_consume_team_queue(monkeypatch):
    monkeypatch.setenv("AI_MODE", "rabbit")
    monkeypatch.setenv("MCP_DELEGATION_TRANSPORT", "unconfigured")
    with TestClient(create_app()) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["reason"] == "DELEGATION_NOT_CONFIGURED"
