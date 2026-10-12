from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .config import Settings


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def _async_main(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.command == "doctor":
        from .doctor import run_doctor
        report = await run_doctor(settings, online=args.online)
        _json(report)
        return 0 if all(item["status"] in {"PASS", "NOT RUN", "BLOCKED"} for item in report["checks"]) else 1

    from .runtime import AgentRuntime
    runtime = AgentRuntime(settings)
    await runtime.startup()
    try:
        if args.command == "run":
            run_id = await runtime.create_run(args.task, allowed_files=args.allow_file)
            _json(await runtime.current_state(run_id))
        elif args.command == "history":
            _json(await runtime.history(args.run_id))
        elif args.command == "resume":
            await runtime.resume(args.run_id)
            _json(await runtime.current_state(args.run_id))
        elif args.command == "replay":
            new_run = await runtime.replay(args.run_id, args.checkpoint_id)
            _json(await runtime.current_state(new_run))
        elif args.command == "fork":
            changes = json.loads(args.changes)
            new_run = await runtime.fork(args.run_id, args.checkpoint_id, changes)
            _json(await runtime.current_state(new_run))
        elif args.command == "dataset":
            from .dataset import run_jsonl
            results = await run_jsonl(runtime, Path(args.path).resolve(), limit=args.limit)
            _json(results)
        else:
            raise ValueError(f"unsupported command: {args.command}")
    finally:
        await runtime.shutdown()
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="m1-local")
    sub = result.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Run offline checks; --online adds exactly 3 model calls")
    doctor.add_argument("--online", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("task")
    run.add_argument("--allow-file", action="append", default=[])
    history = sub.add_parser("history")
    history.add_argument("run_id")
    resume = sub.add_parser("resume")
    resume.add_argument("run_id")
    replay = sub.add_parser("replay")
    replay.add_argument("run_id")
    replay.add_argument("checkpoint_id")
    fork = sub.add_parser("fork")
    fork.add_argument("run_id")
    fork.add_argument("checkpoint_id")
    fork.add_argument("--changes", default="{}", help='JSON; only plan and worker_instruction are allowed')
    dataset = sub.add_parser("dataset")
    dataset.add_argument("path")
    dataset.add_argument("--limit", type=int, required=True, help="Explicit cost-control limit")
    serve = sub.add_parser("serve")
    observe = sub.add_parser("observe", help="No-Key local historical observation; execution APIs disabled")
    observe.add_argument("--phoenix-url", default="http://127.0.0.1:6006")
    observe.add_argument("--port", type=int, default=8000)
    observe.add_argument("--store", type=Path, default=None, help="Dedicated import journal directory")
    observe.add_argument("--catalog", type=Path, default=None, help="Operator-owned JSON allowlist; data files remain under data/external")
    return result


def main() -> None:
    args = parser().parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    if args.command == "observe":
        import uvicorn
        from .observation_api import create_observation_app
        uvicorn.run(create_observation_app(endpoint=args.phoenix_url, directory=args.store, port=args.port, catalog_path=args.catalog),
                    host="127.0.0.1", port=args.port, workers=1, reload=False, loop="asyncio")
        return
    if args.command == "serve":
        import uvicorn
        settings = Settings()
        uvicorn.run("m1_local.api:create_app", factory=True, host=settings.app_host, port=settings.app_port, workers=1, reload=False, loop="asyncio")
        return
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()

