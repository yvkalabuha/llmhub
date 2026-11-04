"""T-372 A1: tests can never reach RunPod (name resolution of RunPod hosts is refused)."""

from __future__ import annotations

import socket

import httpx
import pytest

import conftest


@pytest.mark.parametrize(
    "host",
    ["rest.runpod.io", "api.runpod.io", "RUNPOD.IO", "abc123-8080.proxy.runpod.net",
     "runpod.net.", b"rest.runpod.io"],
)  # fmt: skip
def test_getaddrinfo_refused(host: str | bytes) -> None:
    before = len(conftest.BLOCKED_ATTEMPTS)
    with pytest.raises(PermissionError):
        socket.getaddrinfo(host, 443)
    assert len(conftest.BLOCKED_ATTEMPTS) == before + 1


@pytest.mark.parametrize("host", ["notrunpod.io", "runpod.io.example.com", "localhost"])
def test_other_names_not_blocked(host: str) -> None:
    assert not conftest.blocked_host(host)


def test_httpx_cannot_reach_rest_api() -> None:
    with pytest.raises((httpx.ConnectError, PermissionError)):
        httpx.get("https://rest.runpod.io/v1/pods", timeout=2)


async def test_async_httpx_cannot_reach_graphql() -> None:
    async with httpx.AsyncClient(timeout=2) as c:
        with pytest.raises((httpx.ConnectError, PermissionError)):
            await c.post("https://api.runpod.io/graphql", json={"query": "{ myself { id } }"})
