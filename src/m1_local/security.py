from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit


class NavigationBlocked(ValueError):
    pass


def _is_private_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not ip.is_global


async def validate_public_url(
    url: str, allowed_hosts: set[str], fixture_url: str | None = None, *, allow_public_resource: bool = False
) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise NavigationBlocked(f"blocked URL scheme: {parsed.scheme or '(none)'}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise NavigationBlocked("URL has no hostname")
    if fixture_url:
        fixture = urlsplit(fixture_url)
        requested_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        fixture_port = fixture.port or (443 if fixture.scheme == "https" else 80)
        if (
            parsed.scheme == fixture.scheme
            and host == (fixture.hostname or "").lower().rstrip(".")
            and requested_port == fixture_port
        ):
            return
    if not allow_public_resource and host not in allowed_hosts and not any(host.endswith("." + item) for item in allowed_hosts):
        raise NavigationBlocked(f"host is not allowlisted: {host}")
    try:
        addresses = await asyncio.get_running_loop().run_in_executor(
            None, lambda: {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))}
        )
    except socket.gaierror as exc:
        raise NavigationBlocked(f"DNS resolution failed for {host}") from exc
    if not addresses or any(_is_private_address(address) for address in addresses):
        raise NavigationBlocked(f"host resolves to a non-public address: {host}")


def validate_allowed_file(path_text: str, allowed_roots: list[Path], max_bytes: int) -> Path:
    requested = Path(path_text)
    resolved = requested.resolve(strict=True)
    if resolved.suffix.lower() not in {".txt", ".md", ".csv"}:
        raise ValueError("only .txt, .md and .csv are supported")
    if not any(root.resolve() == resolved.parent or root.resolve() in resolved.parents for root in allowed_roots):
        raise ValueError("file is outside the allowed directories")
    if resolved.is_symlink() or not resolved.is_file():
        raise ValueError("symlinks and non-files are not allowed")
    if resolved.stat().st_size > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} byte limit")
    return resolved
