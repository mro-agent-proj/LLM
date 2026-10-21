"""Durable execution checkpoints, isolated from maintenance business tables."""

import fcntl
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import WorkerSettings


@asynccontextmanager
async def open_checkpoints(settings: WorkerSettings, *, initialize: bool = False):
    if settings.checkpoint_url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row

        async with await AsyncConnection.connect(
            settings.checkpoint_url,
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            options="-csearch_path=langgraph",
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT to_regnamespace('langgraph') AS schema")
                if (await cursor.fetchone())["schema"] is None:
                    raise RuntimeError("Create the AI-owned langgraph schema before initializing")
            saver = AsyncPostgresSaver(connection)
            if initialize:
                await saver.setup()
            yield saver
    else:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(
            str(settings.state_dir / "checkpoints.sqlite")
        ) as saver:
            await saver.setup()
            yield saver


@contextmanager
def process_lock(directory: Path):
    """One consumer per persistent state directory; use one deployment replica."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("AI state directory is already in use") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
