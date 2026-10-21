"""uvicorn app.main:app --host 127.0.0.1 --port 8100 --workers 1"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.agent.model import from_env
from app.agent.settings import Settings
from app.server.delegation import HeaderRuntimeFactory
from app.server.journal import RunJournal
from app.server.rabbit import RabbitWorker


def create_app(*, worker_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        app.state.worker = None
        app.state.reason = "RABBIT_DISABLED"
        if worker_factory is not None:
            app.state.worker = worker_factory()
        elif os.getenv("AI_MODE", "disabled") == "rabbit":
            # Leave the consumer off until the team settles the credential carrier.
            if os.getenv("MCP_DELEGATION_TRANSPORT", "unconfigured") != "amqp_headers":
                app.state.reason = "DELEGATION_NOT_CONFIGURED"
            elif not os.getenv("RABBITMQ_URL"):
                app.state.reason = "RABBIT_NOT_CONFIGURED"
            elif os.getenv("LLM_PROVIDER") != "openai" or os.getenv("LLM_MODEL") != "gpt-4o-mini":
                app.state.reason = "MODEL_CONFIGURATION_INVALID"
            else:
                settings = Settings.from_env()
                model = from_env(settings.model_timeout)
                journal = RunJournal(os.getenv("RUN_JOURNAL_PATH", "artifacts/run-journal.sqlite"))
                app.state.worker = RabbitWorker(
                    os.environ["RABBITMQ_URL"], HeaderRuntimeFactory(model, settings), journal
                )
        elif os.getenv("AI_MODE", "disabled") != "disabled":
            raise ValueError("AI_MODE must be disabled or rabbit")
        if app.state.worker:
            await app.state.worker.start()
        try:
            yield
        finally:
            if app.state.worker:
                await app.state.worker.stop()

    app = FastAPI(title="MRO AI server", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        worker = getattr(app.state, "worker", None)
        is_ready = bool(worker and worker.state.connected)
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "not_ready",
                "reason": worker.state.reason
                if worker
                else getattr(app.state, "reason", "STARTING"),
                "busy": bool(worker and worker.state.busy),
            },
        )

    return app


app = create_app()
