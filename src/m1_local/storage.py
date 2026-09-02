from __future__ import annotations

import json
import sqlite3
import threading
import uuid
import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AppStore:
    """Small application metadata/event store; graph state remains in LangGraph SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextlib.contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task TEXT NOT NULL,
                    head_config TEXT,
                    parent_run_id TEXT,
                    source_checkpoint_id TEXT,
                    dataset_case_id TEXT,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, event_id);
                CREATE TABLE IF NOT EXISTS checkpoint_links (
                    execution_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    node_name TEXT,
                    input_checkpoint_id TEXT,
                    output_checkpoint_id TEXT,
                    trace_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def mark_interrupted_runs(self) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE runs SET status='interrupted', updated_at=? WHERE status IN ('queued','running','pausing')",
                (utc_now(),),
            )
            return cursor.rowcount

    def create_run(self, record: dict[str, Any]) -> None:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO runs(run_id,thread_id,branch_id,status,task,head_config,parent_run_id,
                   source_checkpoint_id,dataset_case_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["run_id"], record["thread_id"], record["branch_id"], record.get("status", "queued"),
                    record["task"], json.dumps(record.get("head_config")) if record.get("head_config") else None,
                    record.get("parent_run_id"), record.get("source_checkpoint_id"), record.get("dataset_case_id"), now, now,
                ),
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "head_config", "error", "pause_requested"}
        clean = {key: value for key, value in fields.items() if key in allowed}
        if not clean:
            return
        if "head_config" in clean:
            clean["head_config"] = json.dumps(clean["head_config"])
        clean["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in clean)
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE runs SET {assignments} WHERE run_id=?", (*clean.values(), run_id))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["head_config"] = json.loads(result["head_config"]) if result["head_config"] else None
        result["pause_requested"] = bool(result["pause_requested"])
        return result

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["head_config"] = json.loads(item["head_config"]) if item["head_config"] else None
            item["pause_requested"] = bool(item["pause_requested"])
            result.append(item)
        return result

    def emit(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        created = utc_now()
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO events(run_id,event_type,payload,created_at) VALUES(?,?,?,?)",
                (run_id, event_type, body, created),
            )
            event_id = int(cursor.lastrowid)
        return {"id": event_id, "run_id": run_id, "event": event_type, "data": json.loads(body), "created_at": created}

    def events_after(self, run_id: str, event_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                (run_id, event_id, limit),
            ).fetchall()
        return [
            {"id": row["event_id"], "run_id": row["run_id"], "event": row["event_type"],
             "data": json.loads(row["payload"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def latest_trace_id(self, run_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT trace_id FROM checkpoint_links
                   WHERE run_id=? AND trace_id IS NOT NULL AND trace_id != ''
                   ORDER BY created_at DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        return str(row["trace_id"]) if row else None

    def request_pause(self, run_id: str) -> None:
        self.update_run(run_id, pause_requested=1, status="pausing")
        self.emit(run_id, "pause_requested", {})

    def consume_pause_request(self, run_id: str) -> bool:
        record = self.get_run(run_id)
        if not record or not record["pause_requested"]:
            return False
        self.update_run(run_id, pause_requested=0)
        return True

    def link_execution(self, *, execution_id: str, run_id: str, node_name: str,
                       input_checkpoint_id: str | None, output_checkpoint_id: str | None,
                       trace_id: str | None, status: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO checkpoint_links
                (execution_id,run_id,node_name,input_checkpoint_id,output_checkpoint_id,trace_id,status,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (execution_id, run_id, node_name, input_checkpoint_id, output_checkpoint_id, trace_id, status, utc_now()),
            )


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str, branch_id: str) -> Path:
        safe_run = uuid.UUID(run_id).hex
        safe_branch = uuid.UUID(branch_id).hex
        path = (self.root / safe_run / safe_branch).resolve()
        if self.root not in path.parents:
            raise ValueError("artifact path escaped root")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_text(self, run_id: str, branch_id: str, name: str, content: str, kind: str) -> dict[str, Any]:
        import hashlib
        safe_name = Path(name).name
        path = self.run_dir(run_id, branch_id) / safe_name
        path.write_text(content, encoding="utf-8")
        relative = path.relative_to(self.root).as_posix()
        return {
            "id": str(uuid.uuid4()), "path": relative, "type": kind,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "size": len(content.encode("utf-8")),
        }

    def resolve_id_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError("artifact path escaped root")
        if not path.is_file():
            raise FileNotFoundError(relative)
        return path
