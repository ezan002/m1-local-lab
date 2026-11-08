"""Connectors for LangGraph agents that run in their own process (route 3: take over checkpoints)."""
from .base import PROTOCOL_VERSION, ConnectorConflict, ConnectorError, ConnectorNotFound, ExecutorConnector
from .generic import StudioExecutorConnector
from .m1_legacy import M1LegacyConnector

CONNECTOR_KINDS = {
    "m1_legacy": {
        "kind": "m1_legacy", "name": "M1 LangGraph executor",
        "description": "The original Planner → Orchestrator → workers executor (m1_local.runtime) running in its own process "
                       "with its own checkpoint database. The Studio drives it through its HTTP control API.",
        "launchable": True,
    },
    "studio_executor": {
        "kind": "studio_executor", "name": "Any LangGraph agent (studio-executor SDK)",
        "description": "A LangGraph program you run yourself, wrapped with studio_executor.StudioExecutor and served on a "
                       "loopback port. Needs the URL and the token the agent prints at startup.",
        "launchable": False,
    },
}

__all__ = ["PROTOCOL_VERSION", "CONNECTOR_KINDS", "ConnectorConflict", "ConnectorError", "ConnectorNotFound",
           "ExecutorConnector", "M1LegacyConnector", "StudioExecutorConnector"]
