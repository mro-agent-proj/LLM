"""Execute an aircraft request, resume its investigation, and publish stage results."""

import asyncio
from datetime import UTC, datetime
from typing import Protocol

from langgraph.errors import GraphRecursionError

from app.agent.runtime import AgentRuntime
from app.agent.workflow import build_graph
from app.messages import RunRequest, event


class EventPublisher(Protocol):
    async def __call__(self, event: dict) -> None: ...


class EventDeliveryError(RuntimeError):
    """A checkpointed event remains available for delivery after reconnection."""


class RunConflict(ValueError):
    """A request identifier was reused for different input."""


class MaintenanceService:
    def __init__(self, checkpointer, *, timeout: float = 240):
        self.checkpointer = checkpointer
        self.timeout = timeout

    async def execute(
        self, run: RunRequest, runtime: AgentRuntime, publish: EventPublisher
    ) -> dict:
        graph = build_graph(runtime, checkpointer=self.checkpointer)
        config = {"configurable": {"thread_id": str(run.request_id)}, "recursion_limit": 80}
        snapshot = await graph.aget_state(config)
        if snapshot.values:
            for key in ("tail_number", "aircraft_model", "airline_code", "symptom_text"):
                if snapshot.values.get(key) != getattr(run, key):
                    raise RunConflict("Request content differs from its saved investigation")
        sent = set()

        async def deliver(state: dict) -> None:
            for item in state.get("stage_events", []):
                if item["event_id"] not in sent:
                    try:
                        await publish(item)
                    except Exception as exc:
                        raise EventDeliveryError("Stage event delivery interrupted") from exc
                    sent.add(item["event_id"])

        if snapshot.values:
            await deliver(snapshot.values)
            if _terminal(snapshot.values):
                return _terminal(snapshot.values)["payload"]
        previous_events = snapshot.values.get("stage_events", [])
        elapsed = 0.0
        if previous_events:
            started = datetime.fromisoformat(
                previous_events[0]["occurred_at"].replace("Z", "+00:00")
            )
            elapsed = max(0, (datetime.now(UTC) - started).total_seconds())
        try:
            async with asyncio.timeout(max(0, self.timeout - elapsed)):
                async for state in graph.astream(
                    None if snapshot.values else run.initial_state(),
                    config,
                    stream_mode="values",
                    durability="sync",
                ):
                    await deliver(state)
        except EventDeliveryError:
            raise
        except Exception as exc:
            # Never leak model responses, URLs, credentials, or raw provider exception text.
            code = (
                "RUN_TIMEOUT"
                if isinstance(exc, TimeoutError)
                else "GRAPH_LIMIT"
                if isinstance(exc, GraphRecursionError)
                else "EXECUTION_FAILED"
            )
            saved = await graph.aget_state(config)
            events = list(saved.values.get("stage_events", []))
            events.append(event(str(run.request_id), "FAILED", {"error": code}))
            await graph.aupdate_state(config, {"stage_events": events})
            await deliver({"stage_events": events})
            return events[-1]["payload"]
        saved = await graph.aget_state(config)
        terminal = _terminal(saved.values)
        if terminal is None:
            raise RuntimeError("Investigation stopped without a terminal event")
        await deliver(saved.values)
        return terminal["payload"]


def _terminal(state: dict) -> dict | None:
    return next(
        (
            e
            for e in reversed(state.get("stage_events", []))
            if e["type"] in {"COMPLETED", "FAILED"}
        ),
        None,
    )
