"""T-369 review cycle 3: FN-1 `x-should-retry` on the generic error paths (500, busy, 413)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from api_fixtures import ApiEnv, chat_body
from llmhub.persistence.db import Busy


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


@pytest.mark.parametrize(
    ("exc", "status", "retry"),
    [(RuntimeError("x"), 500, "true"), (Busy("database is locked"), 503, "true")],
)
async def test_generic_errors_carry_should_retry(
    env: ApiEnv, monkeypatch: pytest.MonkeyPatch, exc: Exception, status: int, retry: str
) -> None:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise exc

    monkeypatch.setattr(env.rt.submitter, "submit", boom)
    r = await env.post("/v1/chat/completions", chat_body())
    assert r.status_code == status
    assert r.headers["x-should-retry"] == retry


async def test_413_and_404_say_do_not_retry(env: ApiEnv) -> None:
    big = "x" * (env.cfg.api.max_body_bytes + 10)
    r = await env.post("/v1/chat/completions", chat_body(big))
    assert r.status_code == 413 and r.headers["x-should-retry"] == "false"
    r = await env.get("/v1/tasks/nope")
    assert r.status_code == 404 and r.headers["x-should-retry"] == "false"
