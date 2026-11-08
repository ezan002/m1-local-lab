"""Start the original M1 executor (AgentRuntime + its HTTP control API) as a separate process.

Agent Studio launches this when the user clicks "Start executor". Nothing here re-implements the
executor: it builds ``m1_local.api.create_app`` around ``AgentRuntime`` with an isolated data directory,
and points the executor's own OpenTelemetry exporter at the Studio so model/node spans can be correlated.

    python -m m1_local.studio.connectors.m1_legacy_host --port 8010 --data-dir data/connected/m1-legacy \
        --model rule_based --otlp-endpoint http://127.0.0.1:8100/v1/traces
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def build_app(port: int, data_dir: Path, model: str, otlp_endpoint: str | None, studio_url: str | None):
    from ...api import create_app
    from ...config import Settings
    from ...runtime import AgentRuntime

    data_dir = data_dir.resolve()
    overrides = {
        "app_port": port,
        "checkpoint_db": data_dir / "checkpoints.sqlite",
        "metadata_db": data_dir / "app.sqlite",
        "artifact_root": data_dir / "artifacts",
    }
    if otlp_endpoint:
        overrides["phoenix_otlp_endpoint"] = otlp_endpoint
    if studio_url:
        overrides["phoenix_base_url"] = studio_url  # only used for a reachability warning
    settings = Settings(**overrides)
    gateway = None
    if model == "rule_based":
        from ...models import RuleBasedGateway
        gateway = RuleBasedGateway()
    elif not settings.openai_api_key.get_secret_value():
        raise SystemExit("OPENAI_API_KEY is not set; start with --model rule_based or set the key in your shell")
    runtime = AgentRuntime(settings, gateway, enable_tracing=bool(otlp_endpoint))
    return create_app(settings, runtime), settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="m1_legacy_host")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model", choices=["openai", "rule_based"], default="rule_based")
    parser.add_argument("--otlp-endpoint", default=None)
    parser.add_argument("--studio-url", default=None)
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    import uvicorn

    app, settings = build_app(args.port, args.data_dir, args.model, args.otlp_endpoint, args.studio_url)
    uvicorn.run(app, host="127.0.0.1", port=settings.app_port, workers=1, reload=False, loop="asyncio", access_log=False)


if __name__ == "__main__":
    main()
