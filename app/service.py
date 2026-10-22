"""Execute an aircraft request, resume its investigation, and publish stage results."""

import asyncio
import logging
import traceback
from datetime import UTC, datetime
from typing import Protocol

from langgraph.errors import GraphRecursionError

from app.agent.full_workflow import build_full_graph
from app.agent.runtime import AgentRuntime
from app.messages import RunRequest, event

logger = logging.getLogger(__name__)


def _where(exc: BaseException) -> str:
    """Last code locations of the failure, never its message (it may hold model output or URLs)."""
    frames = traceback.extract_tb(exc.__traceback__)[-4:]
    places = (f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in reversed(frames))
    return " <- ".join(places)


class EventPublisher(Protocol):
    async def __call__(self, event: dict) -> None: ...


class EventDeliveryError(RuntimeError):
    """A checkpointed event remains available for delivery after reconnection."""


class RunConflict(ValueError):
    """A request identifier was reused for different input."""


class MaintenanceService:
    def __init__(
        self,
        checkpointer,
        *,
        timeout: float = 280,
        graph_factory=build_full_graph,
        timeout_is_remaining=False,
    ):
        self.checkpointer = checkpointer
        self.timeout = timeout
        self.graph_factory = graph_factory
        self.timeout_is_remaining = timeout_is_remaining
        self.workflow_key = "llm123-v1" if graph_factory is build_full_graph else "llm23-v1"

    async def execute(
        self, run: RunRequest, runtime: AgentRuntime, publish: EventPublisher
    ) -> dict:
        graph = self.graph_factory(runtime, checkpointer=self.checkpointer)
        config = {
            "configurable": {"thread_id": f"{self.workflow_key}:{run.request_id}"},
            "recursion_limit": 80,
        }
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
        if previous_events and not self.timeout_is_remaining:
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
                else "AI_EXECUTION_FAILED"
            )
            logger.error(
                "run %s failed with %s (%s) at %s",
                run.request_id, code, type(exc).__name__, _where(exc),
            )
            saved = await graph.aget_state(config)
            if _terminal(saved.values) is not None:
                raise EventDeliveryError("Terminal event delivery is uncertain") from None
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
