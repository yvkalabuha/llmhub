"""HTTP client of the llmhub API used by the MCP server (T-373 plan §3.7).

Only `/v1/tasks*` and `/v1/models`. Error codes are plain strings (no import of
`llmhub.api_gateway`). The key is read through a callable on every request and goes only into
the `Authorization` header; errors and logs carry codes, never exception text or URLs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import httpx

from llmhub.mcp_server.secret import KeyUnavailable, Secret

log = logging.getLogger("llmhub.mcp")

TERMINAL = frozenset({"completed", "failed", "cancelled"})
MAX_RETRIES = 2
DEFAULT_BUDGET_S = 5.0
USER_AGENT = "llmhub-mcp"


@dataclass
class HubError(Exception):
    code: str
    reason: str | None = None
    retryable: bool = False
    retry_after_s: float | None = None
    task_id: str | None = None
    http_status: int | None = None

    def __str__(self) -> str:
        return f"{self.code}" + (f" ({self.reason})" if self.reason else "")


@dataclass(frozen=True)
class Submitted:
    id: str
    status: str
    duplicate: bool


@dataclass(frozen=True)
class TaskResult:
    body: dict[str, Any]
    backend: str | None
    fallback: str | None
    truncated: bool
    warnings: tuple[str, ...]


def _float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_error(resp: httpx.Response) -> HubError:
    should = resp.headers.get("x-should-retry")
    header_after = _float(resp.headers.get("Retry-After"))
    try:
        data = resp.json()
    except ValueError:
        data = None
    err = data.get("error") if isinstance(data, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(err, dict) or not isinstance(err.get("code"), str):  # pyright: ignore[reportUnknownMemberType]
        retryable = should == "true" if should is not None else resp.status_code in (502, 503, 504)
        return HubError("bad_response", f"http_{resp.status_code}", retryable, header_after,
                        None, resp.status_code)  # fmt: skip
    e = cast(dict[str, Any], err)
    retryable = should == "true" if should is not None else bool(e.get("retryable"))
    after = _float(e.get("retry_after_s"))
    return HubError(
        code=e["code"], reason=_str(e.get("reason")), retryable=retryable,
        retry_after_s=after if after is not None else header_after,
        task_id=_str(e.get("task_id")), http_status=resp.status_code,
    )  # fmt: skip


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        raise HubError("bad_response", "not_json", http_status=resp.status_code) from None
    if not isinstance(data, dict):
        raise HubError("bad_response", "not_object", http_status=resp.status_code)
    return cast(dict[str, Any], data)


class HubClient:
    def __init__(
        self,
        base_url: str,
        key: Callable[[], Secret],
        *,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = key
        self._sleep = sleep
        self._monotonic = monotonic
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_s, transport=transport,
            headers={"User-Agent": USER_AGENT}, follow_redirects=False,
        )  # fmt: skip

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def monotonic(self) -> Callable[[], float]:
        return self._monotonic

    def _auth(self) -> dict[str, str]:
        try:
            secret = self._key()
        except KeyUnavailable as exc:
            raise HubError("key_unavailable", exc.reason) from None
        return {"Authorization": f"Bearer {secret.reveal()}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        budget_s: float = DEFAULT_BUDGET_S,
        no_retry: frozenset[str] = frozenset(),
    ) -> httpx.Response:
        start = self._monotonic()
        attempt = 0
        while True:
            all_headers = {**(headers or {}), **self._auth()}
            try:
                resp = await self._http.request(method, path, json=json_body, headers=all_headers)
            except httpx.TransportError as exc:
                error = HubError("transport", type(exc).__name__, retryable=True)
            else:
                if resp.is_success:
                    return resp
                error = _parse_error(resp)
            pause = error.retry_after_s if error.retry_after_s is not None else 1.0
            left = budget_s - (self._monotonic() - start)
            if (
                not error.retryable
                or error.code in no_retry
                or attempt >= MAX_RETRIES
                or pause > left
            ):
                raise error
            attempt += 1
            log.warning("llmhub-mcp: retry %d after %s (%.1f s)", attempt, error.code, pause)
            await self._sleep(pause)

    # --- endpoints ----------------------------------------------------------------------
    async def submit(
        self,
        body: dict[str, Any],
        *,
        idem_key: str,
        data_class: str,
        priority: str | None,
        policy: str | None,
        deadline_s: float | None,
        budget_s: float = DEFAULT_BUDGET_S,
    ) -> Submitted:
        headers = {"Idempotency-Key": idem_key, "X-LLMHub-Data-Class": data_class}
        if priority is not None:
            headers["X-LLMHub-Priority"] = priority
        if policy is not None:
            headers["X-LLMHub-Policy"] = policy
        if deadline_s is not None:
            headers["X-LLMHub-Deadline"] = f"{deadline_s:g}"
        resp = await self._request("POST", "/v1/tasks", json_body=body, headers=headers,
                                   budget_s=budget_s)  # fmt: skip
        data = _json_object(resp)
        task_id, status = data.get("id"), data.get("status")
        if not isinstance(task_id, str) or not isinstance(status, str):
            raise HubError("bad_response", "no_task_id", http_status=resp.status_code)
        return Submitted(task_id, status, duplicate=resp.status_code == 200)

    @staticmethod
    def _task_path(task_id: str, suffix: str = "") -> str:
        return f"/v1/tasks/{quote(task_id, safe='')}{suffix}"

    async def status(self, task_id: str, *, budget_s: float = DEFAULT_BUDGET_S) -> dict[str, Any]:
        resp = await self._request("GET", self._task_path(task_id), budget_s=budget_s)
        return _json_object(resp)

    async def result(self, task_id: str, *, budget_s: float = DEFAULT_BUDGET_S) -> TaskResult:
        resp = await self._request(
            "GET", self._task_path(task_id, "/result"), budget_s=budget_s,
            no_retry=frozenset({"task_not_ready"}),
        )  # fmt: skip
        warn = resp.headers.get("X-LLMHub-Warning", "")
        return TaskResult(
            body=_json_object(resp),
            backend=resp.headers.get("X-LLMHub-Backend"),
            fallback=resp.headers.get("X-LLMHub-Fallback"),
            truncated=resp.headers.get("X-LLMHub-Truncated") == "1",
            warnings=tuple(w.strip() for w in warn.split(",") if w.strip()),
        )

    async def cancel(self, task_id: str, *, budget_s: float = DEFAULT_BUDGET_S) -> dict[str, Any]:
        resp = await self._request("POST", self._task_path(task_id, "/cancel"), budget_s=budget_s)
        return _json_object(resp)

    async def models(self, *, budget_s: float = DEFAULT_BUDGET_S) -> list[dict[str, Any]]:
        data = _json_object(await self._request("GET", "/v1/models", budget_s=budget_s))
        items = data.get("data")
        if not isinstance(items, list):
            raise HubError("bad_response", "no_data")
        return [cast(dict[str, Any], m) for m in cast(list[Any], items) if isinstance(m, dict)]

    async def wait(self, task_id: str, wait_s: float) -> dict[str, Any]:
        """Poll the status until it is terminal or `wait_s` is over; returns the last status
        (the caller checks `status in TERMINAL`). Pause = clamp(eta_s/4, 0.5, 5), 2 s without
        an ETA."""
        deadline = self._monotonic() + max(0.0, wait_s)
        while True:
            st = await self.status(task_id)
            if st.get("status") in TERMINAL:
                return st
            left = deadline - self._monotonic()
            if left <= 0:
                return st
            eta = _float(st.get("eta_s"))
            pause = 2.0 if eta is None else min(5.0, max(0.5, eta / 4))
            await self._sleep(min(pause, left))
