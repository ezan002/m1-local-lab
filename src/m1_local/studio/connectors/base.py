"""Executor control protocol: how the Studio drives a LangGraph agent that runs in its own process.

The agent keeps its own runtime, environment, credentials and checkpoint store. A connector is the
Studio-side client that speaks to the agent's control interface. Everything the Studio needs for
checkpoint-level debugging is expressed by the small surface below; a connector declares which parts it
supports (``capabilities``) and which state fields a fork may edit (``fork_fields``).

Checkpoint records returned by ``history`` are *newest first* and use this shape::

    {"checkpoint_id": str, "parent_checkpoint_id": str | None, "created_at": str | None,
     "step": int | None, "source": "input" | "loop" | "update" | "fork" | None,
     "next": [node, ...], "values": {...}, "interrupted": bool}
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

PROTOCOL_VERSION = "studio-executor/1"


class ConnectorError(RuntimeError):
    """The agent process is unreachable or answered outside the protocol (HTTP 502 in the Studio API)."""


class ConnectorConflict(ValueError):
    """The agent refused the request in its current state (HTTP 409), e.g. another run is active."""


class ConnectorNotFound(KeyError):
    """The agent does not know the run or checkpoint (HTTP 404)."""


class ExecutorConnector(ABC):
    kind: str = "abstract"

    @abstractmethod
    async def describe(self) -> dict[str, Any]:
        """{"name", "framework", "graph": {"nodes": [{id, type, label}], "edges": [{source, target, conditional}]},
        "capabilities": {...}, "fork_fields": [{path, type, description, ...}], "input_fields": [{name, label, default}],
        "provider_mode": "openai" | "deterministic" | "unknown"}"""

    @abstractmethod
    async def health(self) -> dict[str, Any]: ...

    @abstractmethod
    async def list_runs(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def start(self, inputs: dict[str, Any]) -> str:
        """Start a run and return the agent's own run id."""

    @abstractmethod
    async def run(self, remote_run_id: str) -> dict[str, Any]:
        """{"status", "error", "output", "thread_id", "parent_run_id", "source_checkpoint_id"}"""

    @abstractmethod
    async def history(self, remote_run_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def pause(self, remote_run_id: str) -> None: ...

    @abstractmethod
    async def resume(self, remote_run_id: str) -> None: ...

    @abstractmethod
    async def fork(self, remote_run_id: str, checkpoint_id: str, changes: dict[str, Any]) -> str:
        """Create a branch from the checkpoint with the edits applied, start it, and return the new run id."""

    @abstractmethod
    async def replay(self, remote_run_id: str, checkpoint_id: str) -> str: ...

    def validate_changes(self, fork_fields: list[dict[str, Any]], changes: dict[str, Any]) -> dict[str, Any]:
        """Check edits against the declared schema before anything is sent to the agent."""
        allowed = {f["path"]: f for f in fork_fields}
        unknown = sorted(set(changes) - set(allowed))
        if unknown:
            raise ValueError(f"fields not editable for this agent: {unknown}; allowed: {sorted(allowed)}")
        for path, value in changes.items():
            spec = allowed[path]
            if spec["type"] == "string":
                if not isinstance(value, str):
                    raise ValueError(f"{path} must be a string")
                if len(value) > spec.get("max_length", 20_000):
                    raise ValueError(f"{path} is longer than {spec.get('max_length', 20_000)} characters")
                if spec.get("choices") and value not in spec["choices"]:
                    raise ValueError(f"{path} must be one of {spec['choices']}")
            elif spec["type"] == "string_list":
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError(f"{path} must be a JSON list of strings")
                if len(value) > spec.get("max_items", 100):
                    raise ValueError(f"{path} has more than {spec.get('max_items', 100)} items")
            elif spec["type"] == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{path} must be a number")
            elif spec["type"] == "boolean":
                if not isinstance(value, bool):
                    raise ValueError(f"{path} must be true or false")
        return changes

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
