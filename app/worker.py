"""CLI entry points. Production run starts the one FastAPI-owned RabbitMQ consumer."""

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

from app.agent.model import from_env
from app.agent.runtime import runtime_for_request
from app.agent.settings import Settings
from app.checkpoints import open_checkpoints, process_lock
from app.config import WorkerSettings
from app.credentials import credentials_for
from app.messages import RunRequest
from app.service import MaintenanceService


async def run_command(args):
    settings = WorkerSettings.from_env()
    with process_lock(settings.state_dir):
        async with open_checkpoints(
            settings, initialize=args.command == "init-checkpoints"
        ) as saver:
            if args.command == "init-checkpoints":
                return
            run = RunRequest.model_validate_json(args.path.read_bytes())
            credentials = await credentials_for(run, {})
            runtime = runtime_for_request(
                model=from_env(Settings.from_env().model_timeout),
                rag_token=credentials.rag_token,
                business_token=credentials.business_token,
            )

            async def print_event(item):
                import json

                print(json.dumps(item, ensure_ascii=False), flush=True)

            await MaintenanceService(saver, timeout=settings.run_timeout).execute(
                run, runtime, print_event
            )


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    parser = argparse.ArgumentParser(description="MRO LLM1/2/3 AI service")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Start the FastAPI AI server and its configured RabbitMQ consumer")
    sub.add_parser(
        "init-checkpoints", help="Initialize a separately provisioned AI checkpoint store"
    )
    command = sub.add_parser("run-file", help="Run LLM1/2/3 with real model/tools outside RabbitMQ")
    command.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        import uvicorn

        uvicorn.run("app.main:app", host="0.0.0.0", port=8100, workers=1)
    else:
        try:
            asyncio.run(run_command(args))
        except Exception as exc:
            parser.exit(1, f"AI command failed: {type(exc).__name__}. Check local configuration.\n")


if __name__ == "__main__":
    main()
