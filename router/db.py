"""Small SQLite store with short, serialized write transactions."""

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from router.dispatch import canonical


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: Path, host_id: str | None = None):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
        statements = (
                """CREATE TABLE IF NOT EXISTS submissions (
                    instruction_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    prompt_id TEXT NOT NULL, created_at INTEGER NOT NULL, parent_cli TEXT NOT NULL,
                    UNIQUE(session_id, prompt_id))""",
                """CREATE TABLE IF NOT EXISTS current_submissions (
                    session_id TEXT PRIMARY KEY, instruction_id TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS authorizations (
                    session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    operation TEXT NOT NULL, payload TEXT NOT NULL,
                    instruction_id TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(session_id, request_id))""",
                """CREATE TABLE IF NOT EXISTS operations (
                    session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    operation TEXT NOT NULL, payload TEXT NOT NULL, task_id TEXT NOT NULL,
                    PRIMARY KEY(session_id, request_id))""",
                """CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS attempts (
                    task_id TEXT NOT NULL, number INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(task_id, number))""",
                """CREATE TABLE IF NOT EXISTS observations (
                    task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS hosts (
                    host_id TEXT PRIMARY KEY, data TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS sessions (
                    host_id TEXT NOT NULL, source TEXT NOT NULL, session_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(host_id, source, session_id))""",
                """CREATE TABLE IF NOT EXISTS session_tasks (
                    host_id TEXT NOT NULL, source TEXT NOT NULL, session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(host_id, source, session_id, task_id))""",
        )
        with self.transaction() as db:
            for statement in statements:
                db.execute(statement)
            if "parent_agent_id" not in {row[1] for row in db.execute("PRAGMA table_info(authorizations)")}:
                db.execute("ALTER TABLE authorizations ADD COLUMN parent_agent_id TEXT")
            observation_columns = [row[1] for row in db.execute("PRAGMA table_info(observations)")]
            if observation_columns != ["task_id", "session_id", "data"]:
                raise ValueError(
                    "Unsupported observations schema; refusing to discard unknown columns"
                )
            if host_id is not None:
                for row in db.execute("SELECT task_id,data FROM observations").fetchall():
                    record = json.loads(row[1])
                    changed = "host_id" not in record or "source" not in record
                    record.setdefault("host_id", host_id)
                    record.setdefault("source", record.get("executor", "claude"))
                    if changed:
                        db.execute("UPDATE observations SET data=? WHERE task_id=?", (canonical(record), row[0]))
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS observations_identity ON observations(
                json_extract(data,'$.host_id'), json_extract(data,'$.source'), session_id,
                json_extract(data,'$.agent_id'))
                WHERE json_extract(data,'$.host_id') IS NOT NULL
                  AND json_extract(data,'$.source') IS NOT NULL
                  AND json_extract(data,'$.agent_id') IS NOT NULL""")
        path.chmod(0o600)

    @contextmanager
    def connect(self, timeout: float = 10) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=timeout)
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={max(0, int(timeout * 1000))}")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, timeout: float = 10) -> Iterator[sqlite3.Connection]:
        with self.connect(timeout) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def get(self, task_id: str, db: sqlite3.Connection | None = None) -> dict:
        if db is None:
            with self.connect() as connection:
                return self.get(task_id, connection)
        row = db.execute("SELECT data FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown task_id")
        return json.loads(row[0])

    def put(self, db: sqlite3.Connection, task: dict) -> None:
        task["updated_at"] = now_ms()
        db.execute("INSERT INTO tasks VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET data=excluded.data",
                   (task["task_id"], task["session_id"], canonical(task)))

    def update(self, task_id: str, change: Callable[[dict], None]) -> dict:
        with self.transaction() as db:
            task = self.get(task_id, db)
            change(task)
            self.put(db, task)
            return task

    def all(self) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks ORDER BY rowid")]

    def save_attempt(self, task: dict) -> None:
        if "attempt_number" not in task:
            return
        evidence = {key: value for key, value in task.items() if key not in {
            "invocation", "review_history", "attempt_history", "session_id", "feedback", "setup_command", "permission_profile"}}
        with self.transaction() as db:
            db.execute("INSERT INTO attempts VALUES(?,?,?) ON CONFLICT(task_id,number) DO UPDATE SET data=excluded.data",
                       (task["task_id"], task["attempt_number"], canonical(evidence)))

    def history(self, task_id: str) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT data FROM attempts WHERE task_id=? ORDER BY number", (task_id,))]
