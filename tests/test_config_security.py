from __future__ import annotations

from pathlib import Path

import pytest

from m1_local.observability import Observability
from m1_local.security import NavigationBlocked, validate_allowed_file, validate_public_url
from m1_local.state import append_unique


def test_safe_config_never_contains_key(settings):
    configured = settings.model_copy(update={"openai_api_key": settings.openai_api_key.__class__("secret-value")})
    rendered = str(configured.safe_summary())
    assert "secret-value" not in rendered
    assert configured.safe_summary()["api_key_configured"] is True


def test_unreachable_phoenix_is_an_explicit_nonfatal_warning(settings):
    unavailable = settings.model_copy(update={
        "phoenix_base_url": "http://127.0.0.1:1",
        "phoenix_otlp_endpoint": "http://127.0.0.1:1/v1/traces",
    })
    observability = Observability(unavailable)
    warning = observability.check_phoenix()
    assert warning is not None
    assert "unreachable" in warning


def test_file_reader_boundary(settings, tmp_path: Path):
    allowed_root = Path(settings.allowed_file_dirs[0])
    allowed = allowed_root / "ok.csv"
    allowed.write_text("a,b\n1,2\n", encoding="utf-8")
    assert validate_allowed_file(str(allowed), [allowed_root], 1000) == allowed.resolve()
    outside = tmp_path / "outside.csv"
    outside.write_text("private", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        validate_allowed_file(str(outside), [allowed_root], 1000)
    unsupported = allowed_root / "bad.exe"
    unsupported.write_bytes(b"MZ")
    with pytest.raises(ValueError, match="only"):
        validate_allowed_file(str(unsupported), [allowed_root], 1000)


def test_reducer_is_idempotent():
    record = {"id": "stable", "content": "once"}
    assert append_unique([record], [record]) == [record]


@pytest.mark.asyncio
async def test_navigation_blocks_file_internal_and_inexact_fixture_exception():
    with pytest.raises(NavigationBlocked, match="scheme"):
        await validate_public_url("file:///C:/secret.txt", {"example.com"})
    with pytest.raises(NavigationBlocked, match="non-public"):
        await validate_public_url("http://127.0.0.1:9000/admin", {"127.0.0.1"})
    with pytest.raises(NavigationBlocked):
        await validate_public_url(
            "http://127.0.0.1:12341/page", {"example.com"}, "http://127.0.0.1:1234"
        )
    await validate_public_url(
        "http://127.0.0.1:1234/page", {"example.com"}, "http://127.0.0.1:1234"
    )
