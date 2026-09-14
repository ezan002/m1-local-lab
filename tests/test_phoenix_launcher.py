from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_launcher():
    path = Path(__file__).resolve().parents[1] / "scripts/serve_phoenix.py"
    spec = importlib.util.spec_from_file_location("project_phoenix_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_phoenix(monkeypatch):
    modules = {name: ModuleType(name) for name in ("phoenix", "phoenix.server", "phoenix.server.app", "phoenix.server.grpc_server", "phoenix.server.main")}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, attr = name.rsplit(".", 1)
            setattr(modules[parent], attr, module)

    class GrpcServer:
        def __init__(self, enqueue_span, tracer_provider=None, enable_prometheus=False, disabled=False, token_store=None, interceptors=None):
            self.disabled = disabled

    modules["phoenix.server.grpc_server"].GrpcServer = GrpcServer
    modules["phoenix.server.app"].GrpcServer = GrpcServer
    return modules


def test_phoenix_launcher_disables_grpc_without_read_only(monkeypatch):
    launcher = load_launcher()
    modules = fake_phoenix(monkeypatch)
    monkeypatch.setattr(launcher, "version", lambda _: "11.35.0")
    launcher.configure_http_only()
    cls = modules["phoenix.server.app"].GrpcServer
    assert cls(None, disabled=False).disabled is True
    assert cls(None, None, False, False).disabled is True


@pytest.mark.parametrize("drift", ["version", "binding", "signature"])
def test_phoenix_launcher_fails_closed_on_incompatible_dependency(monkeypatch, drift):
    launcher = load_launcher()
    modules = fake_phoenix(monkeypatch)
    monkeypatch.setattr(launcher, "version", lambda _: "11.36.0" if drift == "version" else "11.35.0")
    if drift == "binding":
        modules["phoenix.server.app"].GrpcServer = object
    if drift == "signature":
        modules["phoenix.server.app"].GrpcServer = modules["phoenix.server.grpc_server"].GrpcServer = type("Changed", (), {})
    with pytest.raises(RuntimeError, match="unsupported Phoenix"):
        launcher.configure_http_only()


def test_phoenix_launcher_rejects_host_override_before_starting(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setenv("PHOENIX_HOST", "0.0.0.0")
    monkeypatch.setattr(sys, "argv", ["serve_phoenix.py"])
    with pytest.raises(RuntimeError, match="loopback"):
        launcher.main()


def test_phoenix_launcher_uses_fixed_serve_command(monkeypatch, tmp_path):
    launcher = load_launcher()
    modules = fake_phoenix(monkeypatch)
    monkeypatch.setattr(launcher, "version", lambda _: "11.35.0")
    monkeypatch.setenv("PHOENIX_HOST", "127.0.0.1")
    monkeypatch.delenv("PHOENIX_SQL_DATABASE_URL", raising=False)
    monkeypatch.setenv("PHOENIX_WORKING_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["serve_phoenix.py"])
    calls = []
    modules["phoenix.server.main"].main = lambda: calls.append(sys.argv[:])
    launcher.main()
    assert calls == [["serve_phoenix.py", "serve"]]
