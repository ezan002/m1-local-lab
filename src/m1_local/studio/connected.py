"""Connected executors: debug LangGraph agents that run in their own process (route 3).

The Studio never opens a connected agent's checkpoint database. It mirrors each remote run as a Studio
run (``origin='connected'``) and rebuilds the observable picture from the agent's own checkpoint
history: one node span per committed checkpoint, a pause span for interrupts, an error span for a
failed step and a live span for the step in progress. Control (pause, resume, retry, fork, replay)
is forwarded to the agent, and forks/replays become child runs with lineage, so the existing
Compare view and run pages work unchanged. OTLP spans the agent exports with its own run id are
attached to the mirrored run (see ``otlp.ingest``).
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from .connectors import (CONNECTOR_KINDS, ConnectorConflict, ConnectorError, ConnectorNotFound, M1LegacyConnector,
                         StudioExecutorConnector)
from .connectors.base import ExecutorConnector
from .store import StudioStore, now

ROOT = Path(__file__).resolve().parents[3]
REMOTE_ACTIVE = {"queued", "running", "pausing"}
# Fields that change on every execution (ids, counters). They are kept in the checkpoint view but left
# out of node outputs so that Compare reports "same" when only bookkeeping differs.
VOLATILE_KEYS = {"execution_id", "id", "application_run_id", "branch_id", "parent_run_id", "source_checkpoint_id",
                 "model_call_count", "path", "created_at", "browser_resume", "thread_id"}
MAX_TEXT = 4000
_LOOPBACK = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\]):(\d{2,5})$")


def _sid(*parts: str) -> str:
    return "cn_" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def trim(value: Any, depth: int = 0) -> Any:
    """Bound what the Studio stores from an agent's state (long texts, long lists, deep nesting)."""
    if depth > 6:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + f"… [{len(value) - MAX_TEXT} more characters]"
    if isinstance(value, list):
        items = [trim(v, depth + 1) for v in value[:50]]
        return items + [f"… [{len(value) - 50} more items]"] if len(value) > 50 else items
    if isinstance(value, dict):
        return {str(k): trim(v, depth + 1) for k, v in list(value.items())[:80]}
    return value


def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip_volatile(v) for v in value]
    return value


def state_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What a step wrote: changed keys; for append-style lists only the appended items."""
    delta: dict[str, Any] = {}
    missing = object()
    for key, new in after.items():
        old = before.get(key, missing)
        if new == old:
            continue
        if isinstance(new, list) and isinstance(old, list) and new[:len(old)] == old:
            delta[key] = new[len(old):]
        else:
            delta[key] = new
    return delta


def written_keys(item: dict[str, Any], parent: dict[str, Any] | None) -> set[str]:
    """State keys written by the step that produced this checkpoint.

    Executors on the studio-executor SDK report them ("writes": {node: [keys]}, read from LangGraph's task results).
    Older executors do not; then the keys whose value changed since the parent checkpoint are used, which misses a
    write that left a value unchanged."""
    if item.get("source") != "loop" or parent is None:
        return set()
    if isinstance(item.get("writes"), dict):
        return {k for keys in item["writes"].values() for k in keys}
    return set(state_delta(parent.get("values") or {}, item.get("values") or {}))


def levels(edges: list[dict[str, Any]]) -> dict[str, int]:
    """Shortest distance of each node from __start__."""
    level: dict[str, int] = {"__start__": -1}
    frontier = ["__start__"]
    while frontier:
        nxt = []
        for src in frontier:
            for e in edges:
                if e["source"] == src and e["target"] not in level:
                    level[e["target"]] = level[src] + 1
                    nxt.append(e["target"])
        frontier = nxt
    return level


def layout(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Layered left-to-right layout by shortest distance from __start__ (back edges ignored)."""
    level = levels(edges)
    columns: dict[int, list[str]] = {}
    for n in nodes:
        columns.setdefault(level.get(n["id"], max(level.values()) + 1), []).append(n["id"])
    tallest = max((len(v) for v in columns.values()), default=1)
    positions = {}
    for col, ids in columns.items():
        offset = (tallest - len(ids)) * 60
        for i, nid in enumerate(ids):
            positions[nid] = {"x": col * 270, "y": offset + i * 120}
    return positions


def graph_definition(describe: dict[str, Any]) -> dict[str, Any]:
    graph = describe["graph"]
    positions = layout(graph["nodes"], graph["edges"])
    level = levels(graph["edges"])
    ids = {n["id"] for n in graph["nodes"]}
    sub = {"agent": {"role": "LLM step"}, "tool": {"tool": "worker"}}
    return {
        "nodes": [{"id": n["id"], "type": n["type"], "label": n["label"], "position": positions[n["id"]],
                   "config": sub.get(n["type"], {})} for n in graph["nodes"]],
        "edges": [{"id": f"e_{e['source']}_{e['target']}", "source": e["source"], "target": e["target"],
                   "conditional": bool(e.get("conditional")),
                   "back": level.get(e["target"], 0) <= level.get(e["source"], 0)}
                  for e in graph["edges"] if e["source"] in ids and e["target"] in ids],
        "source": graph.get("source"),
    }


class ConnectedService:
    def __init__(self, store: StudioStore, *, studio_port: int, data_root: Path | None = None,
                 poll_interval: float = 0.7) -> None:
        self.store = store
        self.studio_port = studio_port
        self.data_root = data_root or ROOT / "data" / "connected"
        self.poll_interval = poll_interval
        self._clients: dict[str, ExecutorConnector] = {}
        self._describe: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, dict[str, Any]] = {}
        self._polls: dict[str, asyncio.Task[Any]] = {}
        self._sync_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        for run in self.store.list_runs(origin="connected", limit=1000):
            if run["status"] in REMOTE_ACTIVE:
                self._schedule_poll(run["id"])

    async def shutdown(self) -> None:
        for task in list(self._polls.values()):
            task.cancel()
        if self._polls:
            await asyncio.gather(*self._polls.values(), return_exceptions=True)
        for cid in list(self._processes):
            with contextlib.suppress(Exception):
                await self.stop_process(cid)
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()

    # ----------------------------------------------------------- connectors
    def client(self, connector_id: str) -> ExecutorConnector:
        if connector_id not in self._clients:
            record = self._connector(connector_id)
            if record["kind"] == "m1_legacy":
                self._clients[connector_id] = M1LegacyConnector(record["base_url"], provider_mode=self._provider_mode(record))
            elif record["kind"] == "studio_executor":
                self._clients[connector_id] = StudioExecutorConnector(record["base_url"], record["config"].get("token", ""))
            else:
                raise ValueError(f"unsupported connector kind {record['kind']}")
        return self._clients[connector_id]

    @staticmethod
    def _provider_mode(record: dict[str, Any]) -> str:
        if record["mode"] == "launch":
            return "deterministic" if record["config"].get("model") == "rule_based" else "openai"
        return record["config"].get("provider_mode") or "unknown"

    def _connector(self, connector_id: str) -> dict[str, Any]:
        record = self.store.get_connector(connector_id)
        if record is None:
            raise KeyError(f"connector {connector_id} not found")
        return record

    def create_connector(self, *, kind: str, name: str, mode: str, base_url: str | None = None,
                         port: int | None = None, model: str = "rule_based", provider_mode: str = "unknown",
                         token: str | None = None) -> dict[str, Any]:
        if kind not in CONNECTOR_KINDS:
            raise ValueError(f"unknown connector kind {kind}; available: {sorted(CONNECTOR_KINDS)}")
        if kind == "studio_executor":
            if mode != "attach":
                raise ValueError("SDK agents are started by you (python your_agent.py); attach to their URL")
            if not token or len(token.strip()) < 16:
                raise ValueError("token is required: copy it from the agent's startup line or STUDIO_EXECUTOR_TOKEN")
        connector_id = "conn_" + uuid.uuid4().hex[:10]
        if mode == "attach":
            if not base_url or not _LOOPBACK.match(base_url.rstrip("/")):
                raise ValueError("base_url must be a loopback http URL such as http://127.0.0.1:8000")
            config: dict[str, Any] = {"provider_mode": provider_mode}
            if token:
                config["token"] = token.strip()
            url = base_url.rstrip("/")
        elif mode == "launch":
            port = port or 8010
            if not 1024 <= port <= 65535 or port == self.studio_port:
                raise ValueError("port must be between 1024 and 65535 and differ from the Studio port")
            if model not in {"rule_based", "openai"}:
                raise ValueError("model must be rule_based or openai")
            config = {"model": model, "port": port, "data_dir": str(self.data_root / connector_id)}
            url = f"http://127.0.0.1:{port}"
        else:
            raise ValueError("mode must be attach or launch")
        clash = [c for c in self.store.list_connectors() if c["base_url"] == url]
        if clash:
            raise ValueError(f"connector {clash[0]['id']} already uses {url}")
        return self.store.save_connector({"id": connector_id, "kind": kind, "name": name.strip() or CONNECTOR_KINDS[kind]["name"],
                                          "mode": mode, "base_url": url, "config": config})

    async def delete_connector(self, connector_id: str) -> None:
        self._connector(connector_id)
        if connector_id in self._processes:
            await self.stop_process(connector_id)
        client = self._clients.pop(connector_id, None)
        if client:
            await client.aclose()
        self._describe.pop(connector_id, None)
        self.store.delete_connector(connector_id)

    async def status(self, connector_id: str) -> dict[str, Any]:
        record = self._connector(connector_id)
        proc = self._processes.get(connector_id)
        config = {k: ("•••" if k == "token" else v) for k, v in record["config"].items()}  # never return the secret
        info = {**record, "config": config, "process": self._process_view(proc), "reachable": False,
                "describe": self._describe.get(connector_id)}
        try:
            health = await asyncio.wait_for(self.client(connector_id).health(), timeout=3)
            info.update(reachable=True, health=health)
            if info["describe"] is None:
                info["describe"] = await self.describe(connector_id)
        except (ConnectorError, ConnectorNotFound, asyncio.TimeoutError) as exc:
            info["unreachable_reason"] = str(exc) or "timed out"
        info["run_count"] = self.store._query("SELECT COUNT(*) c FROM runs WHERE origin='connected' AND integration=?", (connector_id,))[0]["c"]
        return info

    async def list_status(self) -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(self.status(c["id"]) for c in self.store.list_connectors())))

    async def describe(self, connector_id: str, *, refresh: bool = False) -> dict[str, Any]:
        if refresh or connector_id not in self._describe:
            self._describe[connector_id] = await self.client(connector_id).describe()
        return self._describe[connector_id]

    # ------------------------------------------------------------ process
    @staticmethod
    def _process_view(proc: dict[str, Any] | None) -> dict[str, Any] | None:
        if proc is None:
            return None
        return {k: v for k, v in proc.items() if k not in {"handle", "reader"}}

    async def start_process(self, connector_id: str, *, timeout: float = 45.0) -> dict[str, Any]:
        record = self._connector(connector_id)
        if record["mode"] != "launch":
            raise ValueError("this connector attaches to an agent you started yourself; start it from your own shell")
        current = self._processes.get(connector_id)
        if current and current["handle"].returncode is None:
            raise ValueError("executor process is already running")
        cfg = record["config"]
        if cfg["model"] == "openai" and not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is not set in the Studio's environment; use model rule_based or set the key before starting the Studio")
        studio = f"http://127.0.0.1:{self.studio_port}"
        cmd = [sys.executable, "-m", "m1_local.studio.connectors.m1_legacy_host", "--port", str(cfg["port"]),
               "--data-dir", cfg["data_dir"], "--model", cfg["model"], "--otlp-endpoint", f"{studio}/v1/traces",
               "--studio-url", studio]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")]))
        Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
        handle = await asyncio.create_subprocess_exec(*cmd, cwd=str(ROOT), env=env, stdout=asyncio.subprocess.DEVNULL,
                                                      stderr=asyncio.subprocess.PIPE)
        proc: dict[str, Any] = {"handle": handle, "pid": handle.pid, "status": "starting", "started_at": now(),
                                "command": " ".join(cmd[1:]), "stderr_tail": "", "exit_code": None}
        proc["reader"] = asyncio.create_task(self._read_stderr(proc))
        self._processes[connector_id] = proc
        self._describe.pop(connector_id, None)
        client = self.client(connector_id)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if handle.returncode is not None:
                await asyncio.sleep(0.2)  # let the reader collect the last stderr lines
                proc.update(status="exited", exit_code=handle.returncode)
                raise ConnectorError(f"executor process exited with code {handle.returncode}: {proc['stderr_tail'][-1500:]}")
            try:
                await client.health()
                proc["status"] = "running"
                await self.describe(connector_id, refresh=True)
                return self._process_view(proc)  # type: ignore[return-value]
            except ConnectorError:
                await asyncio.sleep(0.4)
        await self.stop_process(connector_id)
        raise ConnectorError(f"executor did not answer within {timeout:.0f}s: {proc['stderr_tail'][-1500:]}")

    async def _read_stderr(self, proc: dict[str, Any]) -> None:
        stream = proc["handle"].stderr
        while True:
            line = await stream.readline()
            if not line:
                break
            proc["stderr_tail"] = (proc["stderr_tail"] + line.decode("utf-8", "replace"))[-6000:]
        code = await proc["handle"].wait()
        proc.update(exit_code=code, status="exited" if proc["status"] != "stopped" else "stopped")

    async def stop_process(self, connector_id: str) -> dict[str, Any] | None:
        proc = self._processes.get(connector_id)
        if proc is None:
            raise ValueError("no executor process was started by the Studio for this connector")
        handle = proc["handle"]
        if handle.returncode is None:
            proc["status"] = "stopped"
            handle.terminate()
            try:
                await asyncio.wait_for(handle.wait(), timeout=10)
            except asyncio.TimeoutError:
                handle.kill()
                await handle.wait()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc["reader"], timeout=2)
        proc.update(status="stopped", exit_code=handle.returncode)
        return self._process_view(proc)

    # ---------------------------------------------------------------- runs
    async def start_run(self, connector_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        describe = await self.describe(connector_id)
        allowed = {f["name"] for f in describe["input_fields"]}
        unknown = sorted(set(inputs) - allowed)
        if unknown:
            raise ValueError(f"unknown inputs {unknown}; this agent accepts {sorted(allowed)}")
        remote_id = await self.client(connector_id).start(inputs)
        run = await self._mirror(connector_id, remote_id, inputs=inputs)
        return run

    async def remote_runs(self, connector_id: str) -> list[dict[str, Any]]:
        items = await self.client(connector_id).list_runs()
        for item in items:
            mirrored = self.store.find_remote_run(connector_id, item["run_id"])
            item["studio_run_id"] = mirrored["id"] if mirrored else None
        return items

    async def import_run(self, connector_id: str, remote_run_id: str) -> dict[str, Any]:
        existing = self.store.find_remote_run(connector_id, remote_run_id)
        if existing:
            return existing
        info = await self.client(connector_id).run(remote_run_id)
        parent = self.store.find_remote_run(connector_id, info["parent_run_id"]) if info.get("parent_run_id") else None
        if info.get("parent_run_id") and parent is None:
            parent = await self.import_run(connector_id, info["parent_run_id"])
        relation = info.get("relation")
        if parent is not None and relation is None:
            history = await self.client(connector_id).history(remote_run_id)
            seed = next((h for h in history if h["source"] == "update"), None)
            # The executor seeds forks with edited fields and replays without; both are written as an update.
            relation = "fork" if seed and self._edited_fields(seed, history) else "replay"
        return await self._mirror(connector_id, remote_run_id, parent=parent, relation=relation,
                                  source_checkpoint_id=info.get("source_checkpoint_id"))

    @staticmethod
    def _edited_fields(seed: dict[str, Any], history: list[dict[str, Any]]) -> list[str]:
        parent = next((h for h in history if h["checkpoint_id"] == seed["parent_checkpoint_id"]), None)
        if parent is None:
            return []
        bookkeeping = {"application_run_id", "branch_id", "execution_id", "parent_run_id", "source_checkpoint_id", "browser_resume"}
        return sorted(k for k in state_delta(parent["values"], seed["values"]) if k not in bookkeeping)

    async def _mirror(self, connector_id: str, remote_id: str, *, inputs: dict[str, Any] | None = None,
                      parent: dict[str, Any] | None = None, relation: str | None = None,
                      source_checkpoint_id: str | None = None, fork_changes: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self._connector(connector_id)
        describe = await self.describe(connector_id)
        info = await self.client(connector_id).run(remote_id)
        caps = {**describe["capabilities"], "connected": True, "connector_id": connector_id, "connector_kind": record["kind"],
                "fork_fields": describe["fork_fields"], "control": "remote executor"}
        run = self.store.create_run(
            origin="connected", integration=connector_id, remote_run_id=remote_id,
            workflow_name=f"{record['name']}", thread_id=info.get("thread_id"), definition=graph_definition(describe),
            status=info.get("status") or "queued", input=inputs or info.get("inputs") or {"task": info.get("task")},
            provider_mode=describe.get("provider_mode", "unknown"), capabilities=caps,
            parent_run_id=parent["id"] if parent else None, relation=relation,
            source_checkpoint_id=source_checkpoint_id, fork_changes=fork_changes,
            started_at=info.get("created_at") or now(),
        )
        self.store.insert_span(run["id"], {
            "id": f"cr_{run['id']}", "trace_id": run["id"], "name": f"{record['name']} run", "kind": "workflow",
            "status": "running", "input": run["input"], "start_time": run["started_at"],
            "attributes": {"origin": "connected", "connector": connector_id, "remote_run_id": remote_id,
                           "relation": relation, "provider_mode": run["provider_mode"]},
        })
        await self.sync(run["id"])
        self._schedule_poll(run["id"])
        return self.store.get_run(run["id"])  # type: ignore[return-value]

    def _schedule_poll(self, run_id: str) -> None:
        if run_id in self._polls and not self._polls[run_id].done():
            return
        task = asyncio.create_task(self._poll(run_id))
        self._polls[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self._polls.pop(rid, None) if self._polls.get(rid) is _t else None)

    async def _poll(self, run_id: str) -> None:
        failures = 0
        while True:
            try:
                run = await self.sync(run_id)
                failures = 0
            except (ConnectorError, ConnectorNotFound) as exc:
                failures += 1
                if failures >= 20:
                    self.store.update_run(run_id, error={"type": "ConnectorUnavailable", "message": str(exc),
                                                         "retry": "Start the executor again; the Studio re-syncs when you open the run."})
                    return
                await asyncio.sleep(self.poll_interval * 2)
                continue
            if run["status"] not in REMOTE_ACTIVE:
                return
            await asyncio.sleep(self.poll_interval)

    def is_polling(self, run_id: str) -> bool:
        task = self._polls.get(run_id)
        return bool(task and not task.done())

    async def wait(self, run_id: str, timeout: float = 60.0) -> dict[str, Any]:
        task = self._polls.get(run_id)
        if task:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return self.store.get_run(run_id)  # type: ignore[return-value]

    # ----------------------------------------------------------------- sync
    async def sync(self, run_id: str) -> dict[str, Any]:
        lock = self._sync_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self._require(run_id)
            client = self.client(run["integration"])
            describe = await self.describe(run["integration"])
            info = await client.run(run["remote_run_id"])
            history = await client.history(run["remote_run_id"])
            self._write_spans(run, describe, info, history)
            status = info.get("status") or run["status"]
            fields: dict[str, Any] = {"status": status, "output": info.get("output")}
            if info.get("error"):
                head = history[0] if history else None
                err = info["error"] if isinstance(info["error"], dict) else {"message": str(info["error"])}
                fields["error"] = {"type": err.get("type") or "ExecutorError", "message": str(err.get("message")),
                                   "traceback": err.get("traceback"),
                                   "node_id": (head or {}).get("next", [None])[0] if head and head["next"] else None,
                                   "retry": "Retry asks the executor to resume from its last committed checkpoint."}
            elif status in REMOTE_ACTIVE | {"completed", "partial"}:
                fields["error"] = None  # keep notes such as "branch created but not started" while paused
            if run.get("parent_run_id") and run.get("source_checkpoint_id") and not run.get("inherited"):
                fields["inherited"] = self._inherited(history, run["source_checkpoint_id"], describe)
            if status not in REMOTE_ACTIVE:
                fields["ended_at"] = run.get("ended_at") or now()
                root = f"cr_{run_id}"
                span_status = {"completed": "ok", "partial": "ok", "failed": "error", "paused": "paused"}.get(status, status)
                self.store.end_span(root, status=span_status, output=info.get("output"),
                                    error=fields.get("error"), checkpoint_id=history[0]["checkpoint_id"] if history else None)
            else:
                fields["ended_at"] = None
                with self.store._tx() as c:
                    c.execute("UPDATE spans SET status='running', end_time=NULL WHERE id=?", (f"cr_{run_id}",))
            self.store.update_run(run_id, **fields)
            return self.store.get_run(run_id)  # type: ignore[return-value]

    @staticmethod
    def _own(history: list[dict[str, Any]], source_checkpoint_id: str | None) -> list[dict[str, Any]]:
        """Checkpoints written by this run (newest first): stop at the source checkpoint of a fork/replay."""
        own = []
        for item in history:
            if source_checkpoint_id and item["checkpoint_id"] == source_checkpoint_id:
                break
            own.append(item)
        return own

    def _inherited(self, history: list[dict[str, Any]], source_checkpoint_id: str, describe: dict[str, Any]) -> dict[str, Any]:
        older = []
        found = False
        for item in history:
            if item["checkpoint_id"] == source_checkpoint_id:
                found = True
            if found:
                older.append(item)
        by_id = {h["checkpoint_id"]: h for h in history}
        path = []
        for item in reversed(older):
            parent = by_id.get(item["parent_checkpoint_id"] or "")
            if parent and item["source"] == "loop":
                path.extend(n for n in parent["next"] if n not in {"__start__"})
        return {"path": path, "nodes": sorted(set(path))}

    def _write_spans(self, run: dict[str, Any], describe: dict[str, Any], info: dict[str, Any], history: list[dict[str, Any]]) -> None:
        run_id, root = run["id"], f"cr_{run['id']}"
        types = {n["id"]: n["type"] for n in describe["graph"]["nodes"]}
        labels = {n["id"]: n["label"] for n in describe["graph"]["nodes"]}
        by_id = {h["checkpoint_id"]: h for h in history}
        own = self._own(history, run.get("source_checkpoint_id"))
        for item in reversed(own):
            parent = by_id.get(item["parent_checkpoint_id"] or "")
            if parent is None:
                continue  # the input checkpoint of a fresh run: nothing has executed yet
            attrs = {"checkpoint_source": item["source"], "step": item["step"], "next": item["next"],
                     "input_checkpoint_id": parent["checkpoint_id"], "timing": "checkpoint commit times"}
            delta = state_delta(parent["values"], item["values"])
            if item["source"] == "fork":
                continue  # LangGraph copies the head when a failed/interrupted run is resumed; nothing executed
            if item["source"] == "update":
                edited = sorted(k for k in strip_volatile(delta) if k in {f["path"] for f in describe["fork_fields"]})
                self.store.insert_span(run_id, {
                    "id": _sid(run_id, item["checkpoint_id"], "update"), "trace_id": run_id, "parent_span_id": root,
                    "name": "fork edit" if edited else "replay seed", "kind": "edit", "status": "ok",
                    "input": {"source_checkpoint_id": parent["checkpoint_id"]}, "output": trim(strip_volatile(delta)),
                    "start_time": item["created_at"], "end_time": item["created_at"],
                    "attributes": {**attrs, "edited_fields": edited}, "checkpoint_id": item["checkpoint_id"]})
                continue
            for node in parent["next"]:
                if node == "__start__":
                    continue
                self.store.insert_span(run_id, {
                    "id": _sid(run_id, item["checkpoint_id"], node), "trace_id": run_id, "parent_span_id": root,
                    "node_id": node, "name": labels.get(node, node), "kind": types.get(node, "node"), "status": "ok",
                    "input": {"checkpoint_in": parent["checkpoint_id"]}, "output": trim(strip_volatile(delta)),
                    "start_time": parent["created_at"], "end_time": item["created_at"],
                    "attributes": {**attrs, "node_type": types.get(node, "node")}, "checkpoint_id": item["checkpoint_id"]})
        head = history[0] if history else None
        status = info.get("status")
        # Live, pause and error spans describe the head; recompute them on every sync.
        with self.store._tx() as c:
            c.execute("DELETE FROM spans WHERE run_id=? AND id LIKE 'cl_%'", (run_id,))
            c.execute("UPDATE spans SET status='resumed' WHERE run_id=? AND kind='pause' AND status='paused'", (run_id,))
        if head is None or not head["next"]:
            return
        node = head["next"][0]
        base = {"trace_id": run_id, "parent_span_id": root, "node_id": node, "start_time": head["created_at"],
                "attributes": {"input_checkpoint_id": head["checkpoint_id"], "next": head["next"]}}
        if status == "paused" and head["interrupted"]:
            pause_id = _sid(run_id, head["checkpoint_id"], "pause")
            if not self.store.insert_span(run_id, {**base, "id": pause_id, "name": f"paused before {node}", "kind": "pause", "status": "paused",
                                                   "attributes": {**base["attributes"], "reason": "pause requested"}}):
                with self.store._tx() as c:
                    c.execute("UPDATE spans SET status='paused' WHERE id=?", (pause_id,))
        elif status == "failed":
            err = info.get("error") if isinstance(info.get("error"), dict) else {"message": str(info.get("error"))}
            self.store.insert_span(run_id, {**base, "id": _sid(run_id, head["checkpoint_id"], node, "error", str(err.get("message"))),
                                            "name": labels.get(node, node), "kind": types.get(node, "node"), "status": "error",
                                            "end_time": now(), "error": {"type": err.get("type") or "ExecutorError",
                                                                         "message": str(err.get("message")),
                                                                         "traceback": err.get("traceback")},
                                            "input": {"checkpoint_in": head["checkpoint_id"]}})
        elif status in REMOTE_ACTIVE:
            self.store.insert_span(run_id, {**base, "id": "cl_" + _sid(run_id, head["checkpoint_id"], node)[3:],
                                            "name": labels.get(node, node), "kind": types.get(node, "node"), "status": "running",
                                            "input": {"checkpoint_in": head["checkpoint_id"]}})

    # ---------------------------------------------------------- checkpoints
    async def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        run = self._require(run_id)
        # Re-read the declaration every time: an executor restarted with new fork fields must show them at once.
        describe = await self.describe(run["integration"], refresh=True)
        history = await self.client(run["integration"]).history(run["remote_run_id"])
        by_id = {h["checkpoint_id"]: h for h in history}
        own_ids = {h["checkpoint_id"] for h in self._own(history, run.get("source_checkpoint_id"))}
        labels = {n["id"]: n["label"] for n in describe["graph"]["nodes"]}
        fields = describe["fork_fields"]
        result = []
        for item in history:
            parent = by_id.get(item["parent_checkpoint_id"] or "")
            if item["source"] == "update":
                after = "fork edit"
            elif item["source"] == "fork":
                after = "resume copy"
            elif parent and parent["next"]:
                after = ", ".join(n for n in parent["next"] if n != "__start__") or None
            else:
                after = None
            values = item["values"]
            forkable = bool(item["next"]) and parent is not None and item["next"] != ["__start__"]
            result.append({
                "checkpoint_id": item["checkpoint_id"], "parent_checkpoint_id": item["parent_checkpoint_id"],
                "created_at": item["created_at"], "step": item["step"], "source": item["source"],
                "after_node": after, "after_label": labels.get(after or "", after), "next": item["next"],
                "interrupted": item["interrupted"], "forkable": forkable,
                "inherited": item["checkpoint_id"] not in own_ids,
                "state": trim(values),
                # Plan A: a fork at "after X" may only edit the declared fields that X wrote in this step.
                "editable": [{**f, "value": copy.deepcopy(values.get(f["path"])), "node": after}
                             for f in fields if f["path"] in written_keys(item, parent)] if forkable else [],
            })
        return result

    # -------------------------------------------------------------- control
    def _require(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        if run["origin"] != "connected":
            raise ValueError("not a connected run")
        return run

    async def pause(self, run_id: str) -> dict[str, Any]:
        run = self._require(run_id)
        await self.client(run["integration"]).pause(run["remote_run_id"])
        self._schedule_poll(run_id)
        return {"status": "pause_requested", "note": "takes effect at the executor's next pause gate"}

    async def resume(self, run_id: str) -> dict[str, Any]:
        run = self._require(run_id)
        await self.client(run["integration"]).resume(run["remote_run_id"])
        self.store.update_run(run_id, status="queued", ended_at=None)
        self._schedule_poll(run_id)
        return self.store.get_run(run_id)  # type: ignore[return-value]

    async def fork(self, run_id: str, checkpoint_id: str, changes: dict[str, Any], *, relation: str) -> dict[str, Any]:
        run = self._require(run_id)
        client = self.client(run["integration"])
        describe = await self.describe(run["integration"])
        if relation == "replay":
            if changes:
                raise ValueError("replay does not accept changes; use fork")
            remote_id = await client.replay(run["remote_run_id"], checkpoint_id)
            return await self._mirror(run["integration"], remote_id, parent=run, relation="replay",
                                      source_checkpoint_id=checkpoint_id, inputs=run.get("input"))
        if not changes:
            raise ValueError("fork needs at least one edited field; use replay to continue without changes")
        client.validate_changes(describe["fork_fields"], changes)
        checkpoint = next((c for c in await self.checkpoints(run_id) if c["checkpoint_id"] == checkpoint_id), None)
        if checkpoint is None:
            raise KeyError(f"checkpoint {checkpoint_id} not found in run {run_id}")
        allowed = {e["path"] for e in checkpoint["editable"]}
        if set(changes) - allowed:
            after = checkpoint["after_label"] or checkpoint["after_node"] or "this step"
            raise ValueError(
                f"fields {sorted(set(changes) - allowed)} cannot be edited at the checkpoint after {after!r}: a fork may only "
                f"change what {after!r} wrote ({', '.join(sorted(allowed)) or 'no editable field here'}). To change an "
                "earlier step, fork from the checkpoint after that step.")
        remote_id = await client.fork(run["remote_run_id"], checkpoint_id, changes)
        child = await self._mirror(run["integration"], remote_id, parent=run, relation="fork",
                                   source_checkpoint_id=checkpoint_id, fork_changes=changes, inputs=run.get("input"))
        try:
            await client.resume(remote_id)  # the executor creates forks paused; run the branch now
            self.store.update_run(child["id"], status="queued")
            self._schedule_poll(child["id"])
        except ConnectorConflict as exc:
            self.store.update_run(child["id"], error={"type": "NotStarted", "message": f"branch created but not started: {exc}",
                                                      "retry": "Press Resume when the executor is idle."})
        return self.store.get_run(child["id"])  # type: ignore[return-value]


def connector_error_status(exc: BaseException) -> int:
    if isinstance(exc, ConnectorNotFound) or isinstance(exc, KeyError):
        return 404
    if isinstance(exc, ConnectorError):
        return 502
    return 409


__all__ = ["ConnectedService", "connector_error_status", "graph_definition", "state_delta", "strip_volatile", "trim"]
