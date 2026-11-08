"""Connector for any LangGraph agent exposed with the ``studio_executor`` SDK (protocol studio-executor/1)."""
from __future__ import annotations

from typing import Any

import httpx

from .base import ConnectorConflict, ConnectorError, ConnectorNotFound, ExecutorConnector

TOKEN_HEADER = "x-studio-executor-token"


class StudioExecutorConnector(ExecutorConnector):
    kind = "studio_executor"

    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url + "/studio/v1", timeout=timeout, transport=transport,
                                         trust_env=False, headers={TOKEN_HEADER: token})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"agent at {self.base_url} is unreachable: {type(exc).__name__}: {exc}") from exc
        detail = self._detail(response)
        if response.status_code == 401:
            raise ConnectorError(f"the agent rejected the token: {detail}")
        if response.status_code == 404:
            raise ConnectorNotFound(detail)
        if response.status_code == 409:
            raise ConnectorConflict(detail)
        if response.status_code == 422:
            raise ValueError(detail)
        if response.status_code >= 400:
            raise ConnectorError(f"agent returned HTTP {response.status_code}: {detail}")
        return response.json()

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:500]
        return str(body.get("detail", body))[:500] if isinstance(body, dict) else str(body)[:500]

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def describe(self) -> dict[str, Any]:
        return await self._request("GET", "/describe")

    async def list_runs(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/runs")

    async def start(self, inputs: dict[str, Any]) -> str:
        return (await self._request("POST", "/runs", json={"inputs": inputs}))["run_id"]

    async def run(self, remote_run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/runs/{remote_run_id}")

    async def history(self, remote_run_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/runs/{remote_run_id}/history")

    async def pause(self, remote_run_id: str) -> None:
        await self._request("POST", f"/runs/{remote_run_id}/pause", json={})

    async def resume(self, remote_run_id: str) -> None:
        await self._request("POST", f"/runs/{remote_run_id}/resume", json={})

    async def fork(self, remote_run_id: str, checkpoint_id: str, changes: dict[str, Any]) -> str:
        return (await self._request("POST", f"/runs/{remote_run_id}/fork",
                                    json={"checkpoint_id": checkpoint_id, "changes": changes}))["run_id"]

    async def replay(self, remote_run_id: str, checkpoint_id: str) -> str:
        return (await self._request("POST", f"/runs/{remote_run_id}/replay", json={"checkpoint_id": checkpoint_id}))["run_id"]
