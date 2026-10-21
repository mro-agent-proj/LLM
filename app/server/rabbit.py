"""Consume existing Spring topology. Confirm terminal events before acknowledging requests."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

import aio_pika
from pamqp.commands import Basic

from app.agent.runtime import AgentRuntime
from app.server.contracts import AgentEvent, RunRequest
from app.server.journal import RunJournal
from app.server.runner import EventPublishError, execute_run

LOG = logging.getLogger(__name__)
RuntimeFactory = Callable[[RunRequest, Mapping], Awaitable[AgentRuntime]]


@dataclass
class WorkerState:
    connected: bool = False
    busy: bool = False
    reason: str = "STARTING"


async def publish_confirmed(exchange, event: AgentEvent) -> None:
    body = event.model_dump_json().encode()
    for attempt in range(2):
        try:
            confirmation = await exchange.publish(
                aio_pika.Message(
                    body,
                    content_type="application/json",
                    message_id=event.event_id,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key="agent.event",
                mandatory=True,
                timeout=5,
            )
            if not isinstance(confirmation, Basic.Ack):
                raise EventPublishError("Event was not confirmed")
            return
        except (TimeoutError, ConnectionError, OSError):
            if attempt == 0:
                continue
            raise EventPublishError("Event publish failed") from None
        except asyncio.CancelledError:
            # Shutdown/deadline: leave the delivery unacknowledged, do not emit another outcome.
            raise
        except Exception:
            raise EventPublishError("Event publish failed") from None


async def handle_message(
    message, exchange, factory: RuntimeFactory, *, journal: RunJournal, timeout_seconds=295
):
    try:
        if message.content_type != "application/json" or len(message.body) > 65536:
            raise ValueError("Invalid run envelope")
        request = RunRequest.model_validate_json(message.body)
        if message.message_id != request.request_id:
            raise ValueError("message_id must equal request_id")
        started = journal.begin(request.request_id, message.body)
        stored_events = journal.events(request.request_id)
        # Replay the exact durable terminal decision; never recompute after uncertain publish.
        if any(event.type in {"COMPLETED", "FAILED"} for event in stored_events):
            for event in stored_events:
                await publish_confirmed(exchange, event)
            await message.ack()
            return
        remaining = timeout_seconds - max(0, time.time() - started)
        if message.timestamp:
            timestamp = message.timestamp
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            remaining = min(
                remaining, timeout_seconds - max(0, (datetime.now(UTC) - timestamp).total_seconds())
            )
        if remaining <= 10:
            raise ValueError("Run is already expired")
        async with asyncio.timeout(remaining):
            deadline = asyncio.get_running_loop().time() + remaining
            for event in stored_events:
                await publish_confirmed(exchange, event)
            runtime = await factory(request, message.headers or {})

            async def publish(event):
                durable = journal.save(event)
                await publish_confirmed(exchange, durable)

            execution_budget = min(280, deadline - asyncio.get_running_loop().time() - 10)
            if execution_budget <= 0:
                raise ValueError("Run expired before graph execution")
            await execute_run(request, runtime, publish, timeout_seconds=execution_budget)
        await message.ack()
    except asyncio.CancelledError:
        raise
    except Exception:
        # Includes malformed input, unavailable delegation and unconfirmed terminal publish.
        await message.reject(requeue=False)


class RabbitWorker:
    def __init__(self, url: str, runtime_factory: RuntimeFactory, journal: RunJournal):
        self.url, self.factory = url, runtime_factory
        self.journal = journal
        self.state = WorkerState()
        self.task: asyncio.Task | None = None

    async def start(self):
        self.task = asyncio.create_task(self._serve())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.state.connected = False
        self.state.reason = "STOPPED"

    async def _serve(self):
        while True:
            connection = None
            try:
                connection = await aio_pika.connect(self.url, timeout=5)
                channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                await channel.set_qos(prefetch_count=1)
                # Passive existence checks only; no queue/exchange/binding creation.
                exchange = await channel.get_exchange("mro.agent", ensure=True)
                queue = await channel.get_queue("mro.agent.run", ensure=True)
                self.state.connected, self.state.reason = True, "READY"
                async with queue.iterator() as messages:
                    async for message in messages:
                        self.state.busy = True
                        try:
                            await handle_message(
                                message, exchange, self.factory, journal=self.journal
                            )
                        finally:
                            self.state.busy = False
            except asyncio.CancelledError:
                raise
            except Exception:
                # No exception repr/URL: RabbitMQ URLs may contain credentials.
                LOG.warning("RabbitMQ connection/topology unavailable; retrying")
            finally:
                self.state.connected = False
                self.state.reason = "RABBIT_UNAVAILABLE"
                if connection and not connection.is_closed:
                    with suppress(Exception):
                        await asyncio.wait_for(connection.close(), timeout=5)
            await asyncio.sleep(3)
