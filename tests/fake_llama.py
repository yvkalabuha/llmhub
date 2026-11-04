"""In-process fake llama-server as an ASGI app (T-369 H1). Never listens on a real port.

Endpoints: POST /v1/chat/completions, GET /health, GET /slots, GET /v1/models. Knobs are plain attributes;
delays go through the injected `Clock`, so a FakeClock drives them.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx

from llmhub.clock import Clock

CONTEXT_MSG = "the request exceeds the available context size"


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    json: Any


class FakeLlama:
    def __init__(self, clock: Clock | None = None, *, api_key: str | None = None) -> None:
        self.clock = clock
        self.api_key = api_key
        self.delays: deque[float] = deque()  # one entry per chat call; default 0
        self.chat_actions: deque[int | str] = deque()  # HTTP status, "drop", "context"
        self.health_status = 200
        self.slots_status = 200  # 200 | 404 | 501
        self.external_busy = False
        self.timings = True
        self.tokens_in = 100
        self.tokens_out = 40
        self.pp_tps = 50.0
        self.tg_tps = 4.0
        self.text = "hello"
        self.requests: list[Seen] = []
        self.aborted = 0
        self.in_flight = 0
        self.max_in_flight = 0

    # --- helpers -------------------------------------------------------------
    @property
    def chat_requests(self) -> list[Seen]:
        return [r for r in self.requests if r.path == "/v1/chat/completions"]

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    async def _send(self, send: Any, status: int, body: object) -> None:
        raw = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})  # fmt: skip
        await send({"type": "http.response.body", "body": raw})

    def _chat_body(self, req: dict[str, Any]) -> dict[str, Any]:
        limit = int(req.get("max_tokens") or self.tokens_out)
        out = min(self.tokens_out, limit)
        body: dict[str, Any] = {
            "id": "chatcmpl-fake", "object": "chat.completion", "model": req.get("model"),
            "choices": [{"index": 0, "finish_reason": "length" if out < self.tokens_out else "stop",
                         "message": {"role": "assistant", "content": self.text}}],
            "usage": {"prompt_tokens": self.tokens_in, "completion_tokens": out,
                      "total_tokens": self.tokens_in + out},
        }  # fmt: skip
        if self.timings:
            body["timings"] = {
                "prompt_per_second": self.pp_tps,
                "predicted_per_second": self.tg_tps,
            }
        return body

    # --- ASGI ----------------------------------------------------------------
    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        data = b""
        while True:
            msg = await receive()
            data += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        parsed = json.loads(data) if data else None
        path, method = scope["path"], scope["method"]
        self.requests.append(Seen(method, path, headers, parsed))
        if self.api_key is not None and headers.get("authorization") != f"Bearer {self.api_key}":
            await self._send(send, 401, {"error": {"message": "invalid api key"}})
        elif path == "/health" and method == "GET":
            await self._send(send, self.health_status, {"status": "ok"})
        elif path == "/slots" and method == "GET":
            if self.slots_status != 200:
                await self._send(send, self.slots_status, {"error": {"message": "slots off"}})
            else:
                await self._send(send, 200, [{"id": 0, "is_processing": self.external_busy}])
        elif path == "/v1/models" and method == "GET":
            await self._send(send, self.health_status, {"object": "list", "data": [{"id": "m"}]})
        elif path == "/v1/chat/completions" and method == "POST":
            await self._chat(send, parsed or {})
        else:
            await self._send(send, 404, {"error": {"message": "not found"}})

    async def _chat(self, send: Any, req: dict[str, Any]) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            delay = self.delays.popleft() if self.delays else 0.0
            if delay > 0 and self.clock is not None:
                await self.clock.sleep_until(self.clock.now() + delay)
            action = self.chat_actions.popleft() if self.chat_actions else None
            if action == "drop":
                raise httpx.RemoteProtocolError("connection dropped")
            if action == "context":
                await self._send(send, 400, {"error": {"message": CONTEXT_MSG}})
            elif isinstance(action, int):
                await self._send(send, action, {"error": {"message": "fake failure"}})
            else:
                await self._send(send, 200, self._chat_body(req))
        except asyncio.CancelledError:
            self.aborted += 1
            raise
        finally:
            self.in_flight -= 1
