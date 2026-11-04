"""T-373 C2/C3: HubClient on httpx.MockTransport (answers shaped like docs/openapi.yaml)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from llmhub.mcp_server.client import HubClient, HubError
from llmhub.mcp_server.secret import KeyUnavailable, Secret

KEY = "lh_test_client_key_987654321"
ERROR_TABLE = [
    ("unauthorized", 401, False), ("forbidden", 403, False), ("invalid_request", 400, False),
    ("request_too_large", 413, False), ("context_too_large", 400, False),
    ("model_not_available", 404, False), ("not_found", 404, False),
    ("idempotency_conflict", 409, False), ("task_not_ready", 409, True),
    ("lease_expired", 410, False), ("lease_closed", 409, False), ("conflict", 409, False),
    ("queue_wait_exceeded", 503, True), ("cloud_unavailable", 503, False),
    ("local_unavailable", 503, True), ("controller_halted", 503, False), ("busy", 503, True),
    ("deadline_exceeded", 504, False), ("upstream_timeout", 504, True),
    ("cancelled", 409, False), ("internal_error", 500, True),
]  # fmt: skip


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def monotonic(self) -> float:
        return self.t


def err(code: str, status: int, retryable: bool, *, after: float | None = None,
        header: str | None = None, task_id: str | None = None) -> httpx.Response:  # fmt: skip
    headers = {"x-should-retry": header or ("true" if retryable else "false")}
    if after is not None:
        headers["Retry-After"] = str(int(after))
    body = {"error": {"message": "m", "type": "t", "code": code, "reason": "r",
                      "retryable": retryable, "retry_after_s": after, "task_id": task_id}}  # fmt: skip
    return httpx.Response(status, json=body, headers=headers)


Handler = Callable[[httpx.Request], httpx.Response]


def make(handler: Handler, clock: Clock | None = None,
         key: Callable[[], Secret] | None = None) -> tuple[HubClient, list[httpx.Request]]:  # fmt: skip
    seen: list[httpx.Request] = []
    clock = clock or Clock()

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    client = HubClient(
        "http://hub", key or (lambda: Secret(KEY)), timeout_s=5,
        transport=httpx.MockTransport(wrapped), sleep=clock.sleep, monotonic=clock.monotonic,
    )  # fmt: skip
    return client, seen


async def submit(client: HubClient, **kw: Any) -> Any:
    args: dict[str, Any] = {"idem_key": "mcp-abc", "data_class": "private", "priority": None,
                            "policy": None, "deadline_s": None}  # fmt: skip
    args.update(kw)
    return await client.submit({"model": "auto", "messages": []}, **args)


async def test_submit_202_and_headers() -> None:
    client, seen = make(lambda r: httpx.Response(202, json={"id": "t1", "status": "queued"}))
    s = await submit(client, priority="low", policy="QUALITY", deadline_s=60)
    assert (s.id, s.status, s.duplicate) == ("t1", "queued", False)
    h = seen[0].headers
    assert seen[0].method == "POST" and seen[0].url.path == "/v1/tasks"
    assert h["Authorization"] == f"Bearer {KEY}" and h["Idempotency-Key"] == "mcp-abc"
    assert h["X-LLMHub-Data-Class"] == "private" and h["X-LLMHub-Priority"] == "low"
    assert h["X-LLMHub-Policy"] == "QUALITY" and h["X-LLMHub-Deadline"] == "60"
    assert json.loads(seen[0].content) == {"model": "auto", "messages": []}


async def test_submit_200_is_duplicate_and_no_optional_headers() -> None:
    client, seen = make(lambda r: httpx.Response(200, json={"id": "t1", "status": "running"}))
    assert (await submit(client)).duplicate is True
    assert "X-LLMHub-Priority" not in seen[0].headers and "X-LLMHub-Policy" not in seen[0].headers


@pytest.mark.parametrize(("code", "status", "retryable"), ERROR_TABLE)
async def test_error_table_parsed(code: str, status: int, retryable: bool) -> None:
    client, _ = make(lambda r: err(code, status, retryable, after=200 if retryable else None,
                                   task_id="t9"))  # fmt: skip
    with pytest.raises(HubError) as exc:
        await client.status("t9", budget_s=1)
    e = exc.value
    assert (e.code, e.retryable, e.http_status, e.task_id) == (code, retryable, status, "t9")
    assert e.reason == "r"


async def test_should_retry_false_wins_over_body() -> None:
    client, seen = make(lambda r: err("busy", 503, True, after=1, header="false"))
    with pytest.raises(HubError) as exc:
        await client.status("t", budget_s=30)
    assert exc.value.retryable is False and len(seen) == 1


async def test_busy_retried_with_same_key() -> None:
    answers = [
        err("busy", 503, True, after=1),
        httpx.Response(202, json={"id": "t", "status": "q"}),
    ]
    clock = Clock()
    client, seen = make(lambda r: answers.pop(0), clock)
    assert (await submit(client, budget_s=10)).id == "t"
    assert [r.headers["Idempotency-Key"] for r in seen] == ["mcp-abc", "mcp-abc"]
    assert clock.sleeps == [1.0]


async def test_at_most_two_retries() -> None:
    client, seen = make(lambda r: err("busy", 503, True, after=1))
    with pytest.raises(HubError):
        await client.status("t", budget_s=100)
    assert len(seen) == 3


async def test_retry_after_beyond_budget_no_retry() -> None:
    client, seen = make(lambda r: err("local_unavailable", 503, True, after=60))
    with pytest.raises(HubError) as exc:
        await client.status("t", budget_s=10)
    assert (
        exc.value.code == "local_unavailable" and exc.value.retry_after_s == 60 and len(seen) == 1
    )


async def test_retry_after_header_used_when_body_has_none() -> None:
    resp = httpx.Response(503, json={"error": {"code": "busy", "retryable": True}},
                          headers={"Retry-After": "3"})  # fmt: skip
    answers = [resp, httpx.Response(200, json={"id": "t", "status": "queued"})]
    clock = Clock()
    client, _ = make(lambda r: answers.pop(0), clock)
    await client.status("t", budget_s=10)
    assert clock.sleeps == [3.0]


async def test_transport_error_reason_is_class_name() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach http://hub/?k={KEY}")

    client, seen = make(boom)
    with pytest.raises(HubError) as exc:
        await client.status("t", budget_s=100)
    assert exc.value.code == "transport" and exc.value.reason == "ConnectError"
    assert exc.value.retryable and len(seen) == 3 and KEY not in repr(exc.value)


async def test_401_not_retried() -> None:
    client, seen = make(lambda r: err("unauthorized", 401, False))
    with pytest.raises(HubError) as exc:
        await client.models(budget_s=100)
    assert exc.value.code == "unauthorized" and len(seen) == 1


async def test_non_json_error_is_bad_response() -> None:
    client, _ = make(lambda r: httpx.Response(502, text="<html>gateway</html>"))
    with pytest.raises(HubError) as exc:
        await client.status("t", budget_s=0)
    assert exc.value.code == "bad_response" and exc.value.http_status == 502


async def test_non_json_success_is_bad_response() -> None:
    client, _ = make(lambda r: httpx.Response(200, text="nope"))
    with pytest.raises(HubError) as exc:
        await client.status("t")
    assert exc.value.code == "bad_response"


async def test_key_unavailable() -> None:
    def no_key() -> Secret:
        raise KeyUnavailable("bad_mode")

    client, seen = make(lambda r: httpx.Response(200, json={}), key=no_key)
    with pytest.raises(HubError) as exc:
        await client.models()
    assert (exc.value.code, exc.value.reason, exc.value.retryable) == (
        "key_unavailable",
        "bad_mode",
        False,
    )
    assert seen == []


async def test_result_headers() -> None:
    headers = {"X-LLMHub-Backend": "local:cpu", "X-LLMHub-Fallback": "gpu_unavailable",
               "X-LLMHub-Truncated": "1", "X-LLMHub-Warning": "secret_detected, economy_stage"}  # fmt: skip
    client, seen = make(lambda r: httpx.Response(200, json={"object": "chat.completion"},
                                                 headers=headers))  # fmt: skip
    r = await client.result("t1")
    assert seen[0].url.path == "/v1/tasks/t1/result"
    assert r.body == {"object": "chat.completion"} and r.backend == "local:cpu"
    assert r.fallback == "gpu_unavailable" and r.truncated is True
    assert r.warnings == ("secret_detected", "economy_stage")


async def test_result_not_ready_not_retried() -> None:
    client, seen = make(lambda r: err("task_not_ready", 409, True, after=1))
    with pytest.raises(HubError) as exc:
        await client.result("t", budget_s=100)
    assert exc.value.code == "task_not_ready" and len(seen) == 1


async def test_cancel_and_models_paths() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "auto"}]})
        return httpx.Response(200, json={"id": "t", "status": "cancelled"})

    client, seen = make(h)
    assert (await client.cancel("t"))["status"] == "cancelled"
    assert seen[0].method == "POST" and seen[0].url.path == "/v1/tasks/t/cancel"
    assert await client.models() == [{"id": "auto"}]


async def test_task_id_is_url_quoted() -> None:
    client, seen = make(lambda r: httpx.Response(200, json={"status": "queued"}))
    await client.status("../admin")
    assert seen[0].url.raw_path == b"/v1/tasks/..%2Fadmin"


async def test_key_never_logged_or_in_errors(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = make(lambda r: err("busy", 503, True, after=1))
    with pytest.raises(HubError) as exc:
        await client.status("t", budget_s=100)
    assert KEY not in repr(exc.value) and KEY not in str(exc.value)
    assert KEY not in caplog.text and "Bearer" not in caplog.text


# --- wait (C3) ------------------------------------------------------------------------------
def status_seq(seq: list[dict[str, Any]]) -> Handler:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=seq.pop(0) if len(seq) > 1 else seq[0])

    return h


async def test_wait_until_terminal_with_clamped_pauses() -> None:
    clock = Clock()
    seq = [{"status": "queued", "eta_s": 100}, {"status": "running", "eta_s": 1},
           {"status": "running", "eta_s": 8}, {"status": "running", "eta_s": None},
           {"status": "completed", "eta_s": None}]  # fmt: skip
    client, seen = make(status_seq(seq), clock)
    st = await client.wait("t", 60)
    assert st["status"] == "completed" and len(seen) == 5
    assert clock.sleeps == [5.0, 0.5, 2.0, 2.0]


async def test_wait_times_out_with_last_status() -> None:
    clock = Clock()
    client, seen = make(status_seq([{"status": "queued", "eta_s": 40}]), clock)
    st = await client.wait("t", 12)
    assert st["status"] == "queued" and st["eta_s"] == 40
    assert clock.sleeps == [5.0, 5.0, 2.0] and clock.t == 12 and len(seen) == 4


async def test_wait_zero_polls_once() -> None:
    clock = Clock()
    client, seen = make(status_seq([{"status": "queued", "eta_s": 4}]), clock)
    assert (await client.wait("t", 0))["status"] == "queued"
    assert len(seen) == 1 and clock.sleeps == []
