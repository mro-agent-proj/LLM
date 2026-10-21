"""Local event outbox, separate from the Backend business DB and LangGraph checkpoints."""

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.server.contracts import AgentEvent


class RunJournal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    request_id TEXT PRIMARY KEY, body_hash TEXT NOT NULL, started REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL,
                    body TEXT NOT NULL, terminal INTEGER NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def begin(self, request_id: str, body: bytes) -> float:
        digest = hashlib.sha256(body).hexdigest()
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs VALUES (?, ?, ?)", (request_id, digest, time.time())
            )
            stored_hash, started = conn.execute(
                "SELECT body_hash, started FROM runs WHERE request_id=?", (request_id,)
            ).fetchone()
        if digest != stored_hash:
            raise ValueError("A request id was reused with different execution input")
        return started

    def save(self, event: AgentEvent) -> AgentEvent:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO events(event_id,request_id,body,terminal) VALUES (?,?,?,?)",
                (
                    event.event_id,
                    event.request_id,
                    event.model_dump_json(),
                    int(event.type in {"COMPLETED", "FAILED"}),
                ),
            )
            body = conn.execute(
                "SELECT body FROM events WHERE event_id=?", (event.event_id,)
            ).fetchone()[0]
        return AgentEvent.model_validate_json(body)

    def events(self, request_id: str) -> list[AgentEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT body FROM events WHERE request_id=? ORDER BY sequence", (request_id,)
            ).fetchall()
        return [AgentEvent.model_validate_json(row[0]) for row in rows]
