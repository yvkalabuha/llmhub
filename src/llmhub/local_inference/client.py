"""HTTP client of one local llama-server (OpenAI-compatible, non-streaming; T-369 stream H).

The API key is read by the caller (`api_key_from_env`), lives in the Authorization header only
and never in `repr`, log lines or exception text. Timeouts run on the injected `Clock`, so they
are deterministic on a `FakeClock`.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from llmhub.clock import Clock
from llmhub.configuration.api_models import LocalBackendConfig


class LocalError(Exception):
    """code: local_unavailable | context_too_large | upstream_timeout | bad_response."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass(frozen=True)
class LocalChatResult:
    body: bytes
    tokens_in: int
    tokens_out: int
    finish_reason: str
    prompt_tps: float | None
    gen_tps: float | None
    latency_s: float


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    busy_slots: int
    total_slots: int | None
    status: int | None


async def bounded[T](clock: Clock, aw: Awaitable[T], timeout_s: float) -> T:
    """Await `aw` for at most `timeout_s` of `clock` time; raises TimeoutError."""
    work = asyncio.ensure_future(aw)
    timer = asyncio.ensure_future(clock.sleep_until(clock.now() + timeout_s))
    try:
        done, _ = await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        raise TimeoutError
    finally:
        pending: list[asyncio.Future[Any]] = [t for t in (work, timer) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def api_key_from_env(
    cfg: LocalBackendConfig, environ: Mapping[str, str] | None = None
) -> str | None:
    if cfg.api_key_env is None:
        return None
    value = (os.environ if environ is None else environ).get(cfg.api_key_env)
    if not value:
        raise ValueError(f"environment variable {cfg.api_key_env} is not set")
    return value


def _num(d: object, key: str) -> float | None:
    v = d.get(key) if isinstance(d, dict) else None
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


class LocalBackendClient:
    def __init__(
        self,
        name: str,
        cfg: LocalBackendConfig,
        api_key: str | None,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Clock,
        probe_timeout_s: float = 3.0,
    ) -> None:
        self.name = name
        self.cfg = cfg
        self._api_key = api_key
        self._clock = clock
        self._probe_timeout_s = probe_timeout_s
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s, connect=10.0))
        self._url = cfg.url.rstrip("/")
        self._root = self._url.removesuffix("/v1")
        self.probe_mode: str = cfg.probe

    def __repr__(self) -> str:
        return f"LocalBackendClient(name={self.name!r}, url={self.cfg.url!r})"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def chat(self, request: dict[str, Any], *, timeout_s: float) -> LocalChatResult:
        payload = dict(request)
        payload.pop("stream_options", None)
        payload["stream"] = False
        payload["model"] = self.cfg.model
        t0 = self._clock.now()
        try:
            resp = await bounded(
                self._clock,
                self._http.post(self._url + "/chat/completions", json=payload,
                                headers=self._headers()),
                timeout_s,
            )  # fmt: skip
        except (TimeoutError, httpx.TimeoutException):
            raise LocalError(
                "upstream_timeout", f"{self.name}: no answer in {timeout_s:g}s"
            ) from None
        except httpx.HTTPError as exc:
            raise LocalError("local_unavailable", f"{self.name}: {type(exc).__name__}") from None
        latency = max(0.0, self._clock.now() - t0)
        if resp.status_code == 400:
            text = resp.text.lower()
            if "context" in text or "exceed" in text:
                raise LocalError("context_too_large", f"{self.name}: prompt exceeds the context")
            raise LocalError("bad_response", f"{self.name}: HTTP 400")
        if resp.status_code >= 500 or resp.status_code in (401, 403, 404, 408, 429):
            raise LocalError("local_unavailable", f"{self.name}: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise LocalError("bad_response", f"{self.name}: HTTP {resp.status_code}")
        try:
            data = json.loads(resp.content)
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LocalError("bad_response", f"{self.name}: not a chat.completion") from None
        usage = data.get("usage") if isinstance(data, dict) else None
        timings = data.get("timings") if isinstance(data, dict) else None
        finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        return LocalChatResult(
            body=resp.content,
            tokens_in=int(_num(usage, "prompt_tokens") or 0),
            tokens_out=int(_num(usage, "completion_tokens") or 0),
            finish_reason=str(finish or "stop"),
            prompt_tps=_num(timings, "prompt_per_second"),
            gen_tps=_num(timings, "predicted_per_second"),
            latency_s=latency,
        )

    async def _get(self, path: str) -> httpx.Response | None:
        try:
            return await bounded(
                self._clock,
                self._http.get(self._root + path, headers=self._headers()),
                self._probe_timeout_s,
            )
        except (TimeoutError, httpx.HTTPError):
            return None

    async def probe(self) -> ProbeResult:
        if self.probe_mode == "slots":
            resp = await self._get("/slots")
            if resp is None:
                return ProbeResult(False, 0, None, None)
            if resp.status_code in (404, 501):
                self.probe_mode = "health"  # slots endpoint is off: /health for good
            elif resp.status_code == 200:
                try:
                    slots = resp.json()
                    busy = sum(1 for s in slots if isinstance(s, dict) and s.get("is_processing"))
                    return ProbeResult(True, busy, len(slots), 200)
                except (ValueError, TypeError):
                    return ProbeResult(False, 0, None, 200)
            else:
                return ProbeResult(False, 0, None, resp.status_code)
        resp = await self._get("/health")
        if resp is None:
            return ProbeResult(False, 0, None, None)
        return ProbeResult(resp.status_code == 200, 0, None, resp.status_code)


def build_backends(
    backends: Mapping[str, LocalBackendConfig],
    clock: Clock,
    *,
    probe_timeout_s: float = 3.0,
    environ: Mapping[str, str] | None = None,
    http: httpx.AsyncClient | None = None,
) -> dict[str, LocalBackendClient]:
    """One client per configured backend; keys come from `api_key_env` variables."""
    return {
        n: LocalBackendClient(n, c, api_key_from_env(c, environ), http=http, clock=clock,
                              probe_timeout_s=probe_timeout_s)
        for n, c in backends.items()
    }  # fmt: skip
