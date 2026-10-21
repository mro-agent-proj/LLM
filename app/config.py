"""Deployment settings for the independent maintenance worker."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class WorkerSettings:
    rabbit_url: str = field(repr=False, default="amqp://mro:mro@localhost:5672/")
    run_queue: str = "mro.agent.run"
    exchange: str = "mro.agent"
    event_key: str = "agent.event"
    state_dir: Path = Path(".state")
    checkpoint_url: str | None = field(default=None, repr=False)
    run_timeout: float = 280
    publish_timeout: float = 10
    reconnect_seconds: float = 3

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        settings = cls(
            rabbit_url=os.getenv("RABBITMQ_URL", "amqp://mro:mro@localhost:5672/"),
            run_queue=os.getenv("RUN_QUEUE", "mro.agent.run"),
            exchange=os.getenv("EVENT_EXCHANGE", "mro.agent"),
            event_key=os.getenv("EVENT_ROUTING_KEY", "agent.event"),
            state_dir=Path(os.getenv("STATE_DIR", ".state")),
            checkpoint_url=os.getenv("CHECKPOINT_DATABASE_URL") or None,
            run_timeout=float(os.getenv("RUN_TIMEOUT_SECONDS", "280")),
            publish_timeout=float(os.getenv("PUBLISH_TIMEOUT_SECONDS", "10")),
            reconnect_seconds=float(os.getenv("RECONNECT_SECONDS", "3")),
        )
        if not 0 < settings.run_timeout < 300:
            raise ValueError("RUN_TIMEOUT_SECONDS must be between 0 and 300")
        if settings.publish_timeout <= 0 or settings.reconnect_seconds <= 0:
            raise ValueError("Publish timeout and reconnect interval must be positive")
        return settings
