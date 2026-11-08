"""SQLite persistence for workflows, runs, spans and settings (normalized execution model)."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  definition TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, workflow_id TEXT, workflow_version INTEGER, workflow_name TEXT,
  origin TEXT NOT NULL,            -- studio | external
  integration TEXT,                -- external service name / integration id
  parent_run_id TEXT, source_checkpoint_id TEXT, fork_changes TEXT,
  relation TEXT,                   -- fork | replay | NULL
  thread_id TEXT, definition TEXT, status TEXT NOT NULL,
  input TEXT, output TEXT, error TEXT, provider_mode TEXT,
  capabilities TEXT NOT NULL, pause_requested INTEGER NOT NULL DEFAULT 0,
  breakpoints TEXT NOT NULL DEFAULT '[]',
  inherited TEXT,                  -- fork/replay: path executed before the source checkpoint
  created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_workflow ON runs(workflow_id, created_at);
CREATE TABLE IF NOT EXISTS spans (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, trace_id TEXT, parent_span_id TEXT,
  node_id TEXT, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  input TEXT, output TEXT, error TEXT, attributes TEXT NOT NULL DEFAULT '{}',
  start_time TEXT, end_time TEXT, checkpoint_id TEXT, seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS spans_run ON spans(run_id, seq);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connectors (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
  mode TEXT NOT NULL,              -- attach (agent already running) | launch (Studio starts the agent process)
  base_url TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

_JSON_RUN = ("definition", "input", "output", "error", "capabilities", "fork_changes", "breakpoints", "inherited")
_JSON_SPAN = ("input", "output", "error", "attributes")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class StudioStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)")}
        if "inherited" not in columns:  # additive migration for databases created before this column existed
            self._conn.execute("ALTER TABLE runs ADD COLUMN inherited TEXT")
        if "remote_run_id" not in columns:  # connected runs: the agent's own run id
            self._conn.execute("ALTER TABLE runs ADD COLUMN remote_run_id TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS runs_remote ON runs(integration, remote_run_id)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ---- workflows -------------------------------------------------------
    def list_workflows(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT id,name,description,version,created_at,updated_at FROM workflows ORDER BY updated_at DESC")
        result = [dict(r) for r in rows]
        for item in result:
            last = self._query("SELECT status, created_at FROM runs WHERE workflow_id=? ORDER BY created_at DESC LIMIT 1", (item["id"],))
            item["last_run"] = dict(last[0]) if last else None
            item["run_count"] = self._query("SELECT COUNT(*) c FROM runs WHERE workflow_id=?", (item["id"],))[0]["c"]
        return result

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        if not rows:
            return None
        item = dict(rows[0])
        item["definition"] = _load(item["definition"])
        return item

    def save_workflow(self, workflow_id: str | None, name: str, description: str, definition: dict[str, Any]) -> dict[str, Any]:
        stamp = now()
        with self._tx() as c:
            if workflow_id and c.execute("SELECT 1 FROM workflows WHERE id=?", (workflow_id,)).fetchone():
                c.execute("UPDATE workflows SET name=?, description=?, definition=?, version=version+1, updated_at=? WHERE id=?",
                          (name, description, _dump(definition), stamp, workflow_id))
            else:
                workflow_id = workflow_id or "wf_" + uuid.uuid4().hex[:12]
                c.execute("INSERT INTO workflows VALUES (?,?,?,?,1,?,?)", (workflow_id, name, description, _dump(definition), stamp, stamp))
        return self.get_workflow(workflow_id)  # type: ignore[return-value]

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM workflows WHERE id=?", (workflow_id,)).rowcount > 0

    # ---- runs ------------------------------------------------------------
    def create_run(self, **fields: Any) -> dict[str, Any]:
        stamp = now()
        record = {
            "id": fields.pop("id", None) or "run_" + uuid.uuid4().hex[:12],
            "status": "queued", "capabilities": {}, "breakpoints": [],
            "created_at": stamp, "updated_at": stamp, **fields,
        }
        cols = list(record)
        values = [(_dump(record[k]) if k in _JSON_RUN else record[k]) for k in cols]
        with self._tx() as c:
            c.execute(f"INSERT INTO runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", values)
        return self.get_run(record["id"])  # type: ignore[return-value]

    def update_run(self, run_id: str, **fields: Any) -> None:
        fields["updated_at"] = now()
        sets = ",".join(f"{k}=?" for k in fields)
        values = [(_dump(v) if k in _JSON_RUN else v) for k, v in fields.items()]
        with self._tx() as c:
            c.execute(f"UPDATE runs SET {sets} WHERE id=?", (*values, run_id))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM runs WHERE id=?", (run_id,))
        return self._run(rows[0]) if rows else None

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in _JSON_RUN:
            item[key] = _load(item[key])
        item["pause_requested"] = bool(item["pause_requested"])
        return item

    def list_runs(self, *, workflow_id: str | None = None, origin: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM runs WHERE 1=1", []
        if workflow_id:
            sql += " AND workflow_id=?"
            args.append(workflow_id)
        if origin:
            sql += " AND origin=?"
            args.append(origin)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        result = []
        for row in self._query(sql, tuple(args)):
            item = self._run(row)
            item.pop("definition", None)
            item["span_count"] = self._query("SELECT COUNT(*) c FROM spans WHERE run_id=?", (item["id"],))[0]["c"]
            result.append(item)
        return result

    def children(self, run_id: str) -> list[dict[str, Any]]:
        return [{"id": r["id"], "relation": r["relation"], "status": r["status"], "source_checkpoint_id": r["source_checkpoint_id"], "created_at": r["created_at"]}
                for r in self._query("SELECT id, relation, status, source_checkpoint_id, created_at FROM runs WHERE parent_run_id=? ORDER BY created_at", (run_id,))]

    def mark_interrupted(self) -> list[str]:
        rows = self._query("SELECT id FROM runs WHERE origin='studio' AND status IN ('queued','running','pausing')")
        ids = [r["id"] for r in rows]
        for run_id in ids:
            self.update_run(run_id, status="interrupted", error={"type": "ServiceRestart", "message": "The service stopped while this run was active. Resume continues from the last committed checkpoint."})
        return ids

    # ---- spans -----------------------------------------------------------
    def start_span(self, run_id: str, *, name: str, kind: str, node_id: str | None = None, parent_span_id: str | None = None,
                   input: Any = None, attributes: dict[str, Any] | None = None, trace_id: str | None = None, span_id: str | None = None,
                   start_time: str | None = None, status: str = "running") -> str:
        span_id = span_id or uuid.uuid4().hex[:16]
        with self._tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM spans WHERE run_id=?", (run_id,)).fetchone()[0]
            c.execute("INSERT INTO spans (id,run_id,trace_id,parent_span_id,node_id,name,kind,status,input,attributes,start_time,seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (span_id, run_id, trace_id, parent_span_id, node_id, name, kind, status, _dump(input), _dump(attributes or {}), start_time or now(), seq))
        return span_id

    def end_span(self, span_id: str, *, status: str, output: Any = None, error: Any = None, attributes: dict[str, Any] | None = None,
                 checkpoint_id: str | None = None, end_time: str | None = None) -> None:
        with self._tx() as c:
            row = c.execute("SELECT attributes FROM spans WHERE id=?", (span_id,)).fetchone()
            merged = {**(_load(row["attributes"]) if row else {}), **(attributes or {})}
            c.execute("UPDATE spans SET status=?, output=?, error=?, attributes=?, end_time=?, checkpoint_id=COALESCE(?, checkpoint_id) WHERE id=?",
                      (status, _dump(output), _dump(error), _dump(merged), end_time or now(), checkpoint_id, span_id))

    def set_span_checkpoint(self, span_id: str, checkpoint_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE spans SET checkpoint_id=? WHERE id=?", (checkpoint_id, span_id))

    def insert_span(self, run_id: str, span: dict[str, Any]) -> bool:
        """Idempotent insert of a complete (external) span; returns False if it already existed."""
        with self._tx() as c:
            if c.execute("SELECT 1 FROM spans WHERE id=?", (span["id"],)).fetchone():
                return False
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM spans WHERE run_id=?", (run_id,)).fetchone()[0]
            c.execute("INSERT INTO spans (id,run_id,trace_id,parent_span_id,node_id,name,kind,status,input,output,error,attributes,start_time,end_time,checkpoint_id,seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (span["id"], run_id, span.get("trace_id"), span.get("parent_span_id"), span.get("node_id"), span["name"], span["kind"], span["status"],
                       _dump(span.get("input")), _dump(span.get("output")), _dump(span.get("error")), _dump(span.get("attributes") or {}),
                       span.get("start_time"), span.get("end_time"), span.get("checkpoint_id"), seq))
        return True

    def spans(self, run_id: str) -> list[dict[str, Any]]:
        result = []
        for row in self._query("SELECT * FROM spans WHERE run_id=? ORDER BY seq", (run_id,)):
            item = dict(row)
            for key in _JSON_SPAN:
                item[key] = _load(item[key])
            result.append(item)
        return result

    def find_remote_run(self, connector_id: str, remote_run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM runs WHERE origin='connected' AND integration=? AND remote_run_id=?", (connector_id, remote_run_id))
        return self._run(rows[0]) if rows else None

    # ---- connectors ------------------------------------------------------
    def save_connector(self, record: dict[str, Any]) -> dict[str, Any]:
        stamp = now()
        with self._tx() as c:
            if c.execute("SELECT 1 FROM connectors WHERE id=?", (record["id"],)).fetchone():
                c.execute("UPDATE connectors SET name=?, mode=?, base_url=?, config=?, updated_at=? WHERE id=?",
                          (record["name"], record["mode"], record["base_url"], _dump(record.get("config") or {}), stamp, record["id"]))
            else:
                c.execute("INSERT INTO connectors VALUES (?,?,?,?,?,?,?,?)",
                          (record["id"], record["kind"], record["name"], record["mode"], record["base_url"],
                           _dump(record.get("config") or {}), stamp, stamp))
        return self.get_connector(record["id"])  # type: ignore[return-value]

    def get_connector(self, connector_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM connectors WHERE id=?", (connector_id,))
        if not rows:
            return None
        item = dict(rows[0])
        item["config"] = _load(item["config"])
        return item

    def list_connectors(self) -> list[dict[str, Any]]:
        return [self.get_connector(r["id"]) for r in self._query("SELECT id FROM connectors ORDER BY created_at")]  # type: ignore[misc]

    def delete_connector(self, connector_id: str) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM connectors WHERE id=?", (connector_id,)).rowcount > 0

    # ---- settings --------------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self._query("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
