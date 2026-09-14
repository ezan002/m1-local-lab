"""Local HTTP-only Phoenix launcher for the exact locked server version.

Phoenix 11.35 has no separate gRPC bind-host setting. Disable its unused gRPC
context while retaining writable HTTP OTLP. No installed package is modified.
"""
from __future__ import annotations

import inspect
import os
import sys
from importlib.metadata import version


def configure_http_only() -> None:
    if version("arize-phoenix") != "11.35.0":
        raise RuntimeError("unsupported Phoenix version; review the HTTP-only launcher before upgrading")
    from phoenix.server import app
    from phoenix.server.grpc_server import GrpcServer

    signature = inspect.signature(GrpcServer)
    expected = ("enqueue_span", "tracer_provider", "enable_prometheus", "disabled", "token_store", "interceptors")
    if app.GrpcServer is not GrpcServer or tuple(signature.parameters) != expected:
        raise RuntimeError("unsupported Phoenix gRPC binding/signature; refusing an unguarded listener")

    class HttpOnlyGrpcServer(GrpcServer):
        def __init__(self, *args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.arguments["disabled"] = True
            super().__init__(*bound.args, **bound.kwargs)

    app.GrpcServer = HttpOnlyGrpcServer


def main() -> None:
    if len(sys.argv) != 1:
        raise RuntimeError("this launcher accepts no CLI overrides; use the project startup script")
    if os.environ.get("PHOENIX_HOST", "127.0.0.1") not in {"127.0.0.1", "::1"}:
        raise RuntimeError("Phoenix HTTP must use an explicit loopback address")
    if os.environ.get("PHOENIX_SQL_DATABASE_URL"):
        raise RuntimeError("PHOENIX_SQL_DATABASE_URL overrides local project storage; unset it explicitly before starting")
    os.environ.setdefault("PHOENIX_HOST", "127.0.0.1")
    os.environ["PHOENIX_ALLOW_EXTERNAL_RESOURCES"] = "false"
    configure_http_only()
    from phoenix.server.main import main as phoenix_main

    sys.argv = [sys.argv[0], "serve"]
    phoenix_main()


if __name__ == "__main__":
    main()
