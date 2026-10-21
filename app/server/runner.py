"""Run a bounded full graph; events contain public DTOs, never private MCP context."""

import asyncio
from collections.abc import Awaitable, Callable

from app.agent.full_workflow import build_full_graph
from app.agent.runtime import AgentRuntime
from app.agent.tools import get_context
from app.server.contracts import AgentEvent, RunRequest, make_event

Publish = Callable[[AgentEvent], Awaitable[None]]


class EventPublishError(RuntimeError):
    pass


async def execute_run(
    request: RunRequest, runtime: AgentRuntime, publish: Publish, *, timeout_seconds: float = 280
) -> None:
    async def stage(kind, name, attempt, output):
        payload = {"stage": name, "attempt": attempt}
        if output is not None:
            payload["output"] = output
        await publish(make_event(request.request_id, kind, payload))

    terminal_started = False
    try:
        async with asyncio.timeout(timeout_seconds):
            state = await build_full_graph(runtime, on_stage=stage).ainvoke(request.initial_state())
            context = get_context(state)
            if not context.prepared_payload or context.final_validation == "PENDING":
                raise RuntimeError("Final validation did not complete")
            # COMPLETED publication stays inside the same deadline.
            terminal_started = True
            await publish(make_event(request.request_id, "COMPLETED", context.prepared_payload))
    except EventPublishError:
        # An uncertain COMPLETED publish must not be followed by a contradictory FAILED.
        raise
    except TimeoutError:
        if terminal_started:
            raise EventPublishError("Terminal event confirmation timed out") from None
        await publish(make_event(request.request_id, "FAILED", {"error": "RUN_TIMEOUT"}))
    except Exception:
        if terminal_started:
            raise EventPublishError("Terminal event delivery is uncertain") from None
        # Do not publish exception strings: provider errors can contain credentials or source text.
        await publish(make_event(request.request_id, "FAILED", {"error": "AI_EXECUTION_FAILED"}))
