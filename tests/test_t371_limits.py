"""T-371 C3 (SEC-L4): per-client limit of active jobs -> 429 rate_limited + Retry-After."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from api_fixtures import GAME_KEY, PRIV_KEY, ApiEnv, api_config, chat_body
from llmhub.api_gateway.auth import hash_key
from llmhub.api_gateway.errors import SPEC, ErrorCode
from llmhub.configuration.api_models import ClientConfig


def _cfg(tmp: Path, limit: int) -> object:
    cfg = api_config(tmp)
    cfg.clients["priv"] = ClientConfig(
        key_sha256=hash_key(PRIV_KEY), allowed_models=["*"], max_active_jobs=limit
    )
    return cfg


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path, cfg=_cfg(tmp_path, 2))  # type: ignore[arg-type]
    yield e
    await e.aclose()


def test_spec_entry() -> None:
    assert SPEC[ErrorCode.RATE_LIMITED] == (429, True, "rate_limit_error")


async def test_third_active_job_gets_429(env: ApiEnv) -> None:
    for i in range(2):
        r = await env.post("/v1/tasks", chat_body(f"q{i}"), key=PRIV_KEY)
        assert r.status_code == 202, r.text
    r = await env.post("/v1/tasks", chat_body("q3"), key=PRIV_KEY)
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "rate_limited" and err["retryable"] is True
    assert err["type"] == "rate_limit_error"
    assert int(r.headers["Retry-After"]) >= 1
    assert r.headers["x-should-retry"] == "true"
    assert len(env.jobs()) == 2


async def test_chat_endpoint_is_limited_too(env: ApiEnv) -> None:
    for i in range(2):
        assert (await env.post("/v1/tasks", chat_body(f"q{i}"), key=PRIV_KEY)).status_code == 202
    r = await env.post("/v1/chat/completions", chat_body("sync"), key=PRIV_KEY)
    assert r.status_code == 429


async def test_limit_is_per_client(env: ApiEnv) -> None:
    for i in range(2):
        assert (await env.post("/v1/tasks", chat_body(f"q{i}"), key=PRIV_KEY)).status_code == 202
    r = await env.post("/v1/tasks", chat_body("other", model="big"), key=GAME_KEY,
                       headers={"X-LLMHub-Data-Class": "private"})  # fmt: skip
    assert r.status_code == 202


async def test_idempotent_repeat_at_limit_is_not_429(env: ApiEnv) -> None:
    first = await env.post("/v1/tasks", chat_body("a"), key=PRIV_KEY,
                           headers={"Idempotency-Key": "k-1"})  # fmt: skip
    assert first.status_code == 202
    assert (await env.post("/v1/tasks", chat_body("b"), key=PRIV_KEY)).status_code == 202
    again = await env.post("/v1/tasks", chat_body("a"), key=PRIV_KEY,
                           headers={"Idempotency-Key": "k-1"})  # fmt: skip
    assert again.status_code in (200, 202)
    assert again.json()["id"] == first.json()["id"]


async def test_finished_jobs_free_the_slot(env: ApiEnv) -> None:
    ids = []
    for i in range(2):
        r = await env.post("/v1/tasks", chat_body(f"q{i}"), key=PRIV_KEY)
        ids.append(r.json()["id"])
    await env.pump(30)
    assert all(env.job(i)["status"] == "completed" for i in ids)
    assert (await env.post("/v1/tasks", chat_body("q9"), key=PRIV_KEY)).status_code == 202


# ---------------------------------------------------------------- C4 (SEC-L2): docs flag


async def test_docs_on_by_default(tmp_path: Path) -> None:
    e = ApiEnv(tmp_path)
    try:
        assert (await e.get("/openapi.json", key=None)).status_code == 200
        assert (await e.get("/docs", key=None)).status_code == 200
    finally:
        await e.aclose()


async def test_docs_off(tmp_path: Path) -> None:
    cfg = api_config(tmp_path)
    cfg.api.docs = False
    e = ApiEnv(tmp_path, cfg=cfg)
    try:
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            r = await e.get(path, key=None)
            assert r.status_code == 404, path
            assert r.json()["error"]["code"] == "not_found"
        assert (await e.get("/healthz", key=None)).status_code == 200
    finally:
        await e.aclose()


def test_openapi_lists_rate_limited() -> None:
    import yaml

    spec = yaml.safe_load((Path(__file__).parent.parent / "docs/openapi.yaml").read_text())
    assert "rate_limited" in spec["components"]["schemas"]["ErrorCode"]["enum"]
    for path in ("/v1/chat/completions", "/v1/tasks"):
        assert "429" in spec["paths"][path]["post"]["responses"], path
