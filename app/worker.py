"""RabbitMQ entry point for LLM2 investigation and LLM3 review."""

import argparse
import asyncio
import fcntl
import json
import logging
from contextlib import contextmanager
from pathlib import Path

import aio_pika
from dotenv import load_dotenv
from pydantic import ValidationError

from app.agent.model import from_env
from app.agent.runtime import runtime_for_request
from app.agent.settings import Settings
from app.checkpoints import open_checkpoints
from app.config import WorkerSettings
from app.credentials import credentials_for
from app.messages import RunRequest
from app.service import EventDeliveryError, MaintenanceService, RunConflict

logger = logging.getLogger("mro.worker")


@contextmanager
def process_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("This worker state directory is already in use") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class RabbitPublisher:
    def __init__(self, exchange, settings: WorkerSettings):
        self.exchange, self.settings = exchange, settings

    async def __call__(self, item: dict) -> None:
        # Re-delivery uses the same checkpointed event_id so Spring can deduplicate.
        for attempt in range(3):
            try:
                await self.exchange.publish(
                    aio_pika.Message(
                        json.dumps(item, ensure_ascii=False).encode(),
                        content_type="application/json",
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        message_id=item["event_id"],
                        correlation_id=item["request_id"],
                    ),
                    routing_key=self.settings.event_key,
                    mandatory=True,
                    timeout=self.settings.publish_timeout,
                )
                return
            except Exception:
                if attempt == 2:
                    raise EventDeliveryError("Broker did not confirm event delivery") from None
                await asyncio.sleep(attempt + 1)


async def handle_message(message, service: MaintenanceService, model, publish) -> None:
    try:
        if message.content_type != "application/json":
            raise ValueError("Expected application/json")
        run = RunRequest.model_validate_json(message.body)
        if message.message_id and message.message_id != str(run.request_id):
            raise ValueError("message_id must match request_id")
    except (ValidationError, ValueError):
        logger.warning("Rejected malformed maintenance request")
        await message.reject(requeue=False)
        return
    try:
        credentials = await credentials_for(run, dict(message.headers or {}))
        runtime = runtime_for_request(
            model=model,
            rag_token=credentials.rag_token,
            business_token=credentials.business_token,
        )
        await service.execute(run, runtime, publish)
    except EventDeliveryError:
        # Closing the channel leaves this delivery unacknowledged; reconnect resumes it.
        raise
    except RunConflict:
        logger.warning("Rejected changed input for saved request %s", run.request_id)
        await message.reject(requeue=False)
        return
    except Exception:
        # Configuration/authentication failure before graph execution is a permanent run failure.
        logger.error("Cannot start maintenance request %s", run.request_id)
        await message.reject(requeue=False)
        return
    await message.ack()


async def consume(settings: WorkerSettings, service: MaintenanceService, model) -> None:
    while True:
        connection = None
        try:
            connection = await aio_pika.connect_robust(settings.rabbit_url, timeout=10)
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await channel.set_qos(prefetch_count=1)
            # Existing topology is owned by the backend. Do not declare or alter its queues.
            exchange = await channel.get_exchange(settings.exchange, ensure=True)
            queue = await channel.get_queue(settings.run_queue, ensure=True)
            publish = RabbitPublisher(exchange, settings)
            logger.info("Waiting for maintenance requests on %s", settings.run_queue)
            async with queue.iterator() as messages:
                async for message in messages:
                    await handle_message(message, service, model, publish)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Broker connection interrupted; reconnecting")
        finally:
            if connection is not None:
                await connection.close()
        await asyncio.sleep(settings.reconnect_seconds)


async def run_file(path: Path, settings: WorkerSettings, saver, model) -> None:
    run = RunRequest.model_validate_json(path.read_bytes())
    credentials = await credentials_for(run, {})
    runtime = runtime_for_request(
        model=model, rag_token=credentials.rag_token, business_token=credentials.business_token
    )

    async def print_event(item):
        print(json.dumps(item, ensure_ascii=False), flush=True)

    await MaintenanceService(saver, timeout=settings.run_timeout).execute(run, runtime, print_event)


async def async_main(args) -> None:
    load_dotenv()
    settings = WorkerSettings.from_env()
    with process_lock(settings.state_dir):
        async with open_checkpoints(
            settings, initialize=args.command == "init-checkpoints"
        ) as saver:
            if args.command == "init-checkpoints":
                print("Maintenance worker checkpoints initialized")
                return
            model = from_env(Settings.from_env().model_timeout)
            if args.command == "run-file":
                await run_file(args.path, settings, saver, model)
                return
            await consume(settings, MaintenanceService(saver, timeout=settings.run_timeout), model)


def main() -> None:
    parser = argparse.ArgumentParser(description="Aircraft maintenance investigation and review")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Consume maintenance requests and publish decisions")
    sub.add_parser("init-checkpoints", help="Initialize AI execution checkpoint tables")
    file_parser = sub.add_parser(
        "run-file", help="Investigate a request using real LLM and MCP tools"
    )
    file_parser.add_argument("path", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Provider and HTTP debug logs can contain raw request data; keep them quiet.
    for name in ("httpx", "httpcore", "openai", "aio_pika", "aiormq"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        parser.exit(1, f"Worker startup failed: {type(exc).__name__}. Check local configuration.\n")


if __name__ == "__main__":
    main()
