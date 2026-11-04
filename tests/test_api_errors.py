"""T-369 W1: error table (21 codes + T-371 rate_limited), OpenAI error object, handlers."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from llmhub.api_gateway.errors import SPEC, ApiError, ErrorCode, install_handlers, retryable_for
from llmhub.persistence.db import Busy

TABLE = {
    "unauthorized": (401, False), "forbidden": (403, False), "invalid_request": (400, False),
    "request_too_large": (413, False), "context_too_large": (400, False),
    "model_not_available": (404, False), "not_found": (404, False),
    "idempotency_conflict": (409, False), "task_not_ready": (409, True),
    "lease_expired": (410, False), "lease_closed": (409, False), "conflict": (409, False),
    "queue_wait_exceeded": (503, True), "cloud_unavailable": (503, True),
    "local_unavailable": (503, True), "controller_halted": (503, False), "busy": (503, True),
    "deadline_exceeded": (504, False), "upstream_timeout": (504, True), "cancelled": (409, False),
    "internal_error": (500, True), "rate_limited": (429, True),
}  # fmt: skip


def test_table_has_22_codes() -> None:
    assert len(ErrorCode) == 22
    assert set(TABLE) == {c.value for c in ErrorCode}


@pytest.mark.parametrize("code", list(ErrorCode))
def test_each_row_http_retryable_type(code: ErrorCode) -> None:
    http, retryable = TABLE[code.value]
    assert SPEC[code][0] == http
    assert SPEC[code][1] is retryable
    err = ApiError(code, "m", retry_after_s=7 if retryable else None, task_id="t1")
    assert err.status == http
    body = err.body()["error"]
    assert body["code"] == code.value
    assert body["type"] == SPEC[code][2]
    assert body["retryable"] is retryable
    assert body["param"] is None
    assert body["task_id"] == "t1"
    for key in ("message", "reason", "retry_after_s", "fallback"):
        assert key in body
    if retryable:
        assert err.response_headers()["Retry-After"] == "7"
    else:
        assert "Retry-After" not in err.response_headers()


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [
        ("provider_api_error", True),
        ("gpu_unavailable", True),
        ("deployment_error", True),
        ("insufficient_balance", False),
        ("quota_exceeded", False),
        ("budget_limit_reached", False),
    ],
)
def test_cloud_unavailable_six_reasons(reason: str, retryable: bool) -> None:
    assert retryable_for(ErrorCode.CLOUD_UNAVAILABLE, reason) is retryable
    err = ApiError(ErrorCode.CLOUD_UNAVAILABLE, "x", reason=reason)
    assert err.body()["error"]["retryable"] is retryable
    assert err.body()["error"]["reason"] == reason


class _Item(BaseModel):
    n: int


def _app() -> FastAPI:
    app = FastAPI()
    install_handlers(app)

    @app.get("/api")
    async def api() -> None:
        raise ApiError(ErrorCode.FORBIDDEN, "no", reason="r")

    @app.post("/val")
    async def val(item: _Item) -> None:
        return None

    @app.get("/busy")
    async def busy() -> None:
        raise Busy("locked")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals sk-should-not-leak")

    return app


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_api_error_response() -> None:
    async with await _client() as c:
        r = await c.get("/api")
    assert r.status_code == 403
    assert r.json()["error"]["reason"] == "r"


async def test_validation_error_does_not_echo_body() -> None:
    async with await _client() as c:
        r = await c.post("/val", json={"n": "TOP-SECRET-VALUE"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "TOP-SECRET-VALUE" not in r.text


async def test_unknown_path_and_method() -> None:
    async with await _client() as c:
        r404 = await c.get("/nope")
        r405 = await c.delete("/api")
    assert r404.status_code == 404
    assert r404.json()["error"]["code"] == "not_found"
    assert r405.status_code == 405
    assert r405.json()["error"]["code"] == "invalid_request"


async def test_busy_and_internal_error() -> None:
    async with await _client() as c:
        busy = await c.get("/busy")
        boom = await c.get("/boom")
    assert busy.status_code == 503
    assert busy.json()["error"]["code"] == "busy"
    assert busy.json()["error"]["retry_after_s"] == 1
    assert busy.headers["Retry-After"] == "1"
    assert boom.status_code == 500
    assert boom.json()["error"]["code"] == "internal_error"
    assert "sk-should-not-leak" not in boom.text
