"""Run the checkpointed LLM1/2/3 graph through the single FastAPI RabbitMQ worker."""

from collections.abc import Awaitable, Callable

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runtime import AgentRuntime
from app.server.contracts import AgentEvent, RunRequest
from app.service import EventDeliveryError, MaintenanceService

Publish = Callable[[AgentEvent], Awaitable[None]]
EventPublishError = EventDeliveryError


async def execute_run(
    request: RunRequest,
    runtime: AgentRuntime,
    publish: Publish,
    *,
    timeout_seconds: float = 280,
    checkpointer=None,
) -> None:
    async def deliver(item):
        await publish(AgentEvent.model_validate(item))

    await MaintenanceService(
        checkpointer if checkpointer is not None else InMemorySaver(),
        timeout=timeout_seconds,
        timeout_is_remaining=True,
    ).execute(request, runtime, deliver)
