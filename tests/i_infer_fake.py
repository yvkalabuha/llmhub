"""Fake `llmhub-infer` front (T-370 G3): same contract as `llmhub_infer.front`, with knobs."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

LLAMA_TIMINGS = {
    "prompt_n": 1200, "prompt_ms": 1000.0, "prompt_per_second": 1200.0,
    "predicted_n": 78, "predicted_ms": 1000.0, "predicted_per_second": 78.0,
    "draft_n": 60, "draft_n_accepted": 45,
}  # fmt: skip


def chat_body(*, usage: bool = True, timings: bool = True, choices: int = 1) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {"index": i, "message": {"role": "assistant", "content": "ok"},
             "finish_reason": "length"}
            for i in range(choices)
        ],
    }  # fmt: skip
    if usage:
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    if timings:
        body["timings"] = dict(LLAMA_TIMINGS)
    return body


@dataclass
class FakeInfer:
    key: str
    phase: str = "ok"  # loading | llama_load | ok | failed
    reason: str = "weights"
    progress: dict[str, Any] = field(
        default_factory=lambda: {
            "phase": "downloading",
            "file": "model",
            "source": "self_hosted",
            "done": 100,
            "total": 1000,
            "mb_s": 50.0,
            "failovers": [],
            "eta_s": 18.0,
            "sources_left": 1,
        }
    )
    chat: dict[str, Any] = field(default_factory=chat_body)
    chat_status: int = 200
    chat_raw: bytes | None = None  # raw body instead of `chat`
    chat_content_type: str = "application/json"
    delay_s: float = 0.0  # before the first byte
    stall_after: int | None = None  # send N bytes then stall forever
    drop_after: int | None = None  # send N bytes then break the connection
    proxy_no_pod: bool = False  # RunPod proxy page instead of our front (502 HTML)
    health_html: bool = False  # /health answered by a proxy page
    seen: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)

    def app(self) -> Starlette:
        def authorized(request: Request) -> bool:
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            return scheme.lower() == "bearer" and hmac.compare_digest(
                token.encode(), self.key.encode()
            )

        async def ping(request: Request) -> Response:
            if self.proxy_no_pod:
                return Response("<html>bad gateway</html>", 502, media_type="text/html")
            return Response(status_code=204)

        async def health(request: Request) -> Response:
            if self.proxy_no_pod or self.health_html:
                return Response("<html>no pod</html>", 502, media_type="text/html")
            if not authorized(request):
                return JSONResponse({"error": "unauthorized"}, 401)
            if self.phase == "failed":
                return JSONResponse({"status": "failed", "reason": self.reason}, 500)
            if self.phase in ("loading", "llama_load"):
                body = {"status": "loading", **self.progress}
                if self.phase == "llama_load":
                    body["phase"] = "llama_load"
                return JSONResponse(body, 503)
            return JSONResponse({"status": "ok"})

        async def chat(request: Request) -> Response:
            raw = await request.body()
            self.seen.append((request.url.path, dict(request.headers), raw))
            if self.proxy_no_pod:
                return Response("<html>bad gateway</html>", 502, media_type="text/html")
            if not authorized(request):
                return JSONResponse({"error": "unauthorized"}, 401)
            if self.phase == "failed":
                return JSONResponse({"status": "failed", "reason": self.reason}, 500)
            if self.phase != "ok":
                return JSONResponse({"error": "not ready"}, 503)
            payload = self.chat_raw if self.chat_raw is not None else json.dumps(self.chat).encode()

            async def gen() -> AsyncIterator[bytes]:
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                cut = self.stall_after if self.stall_after is not None else self.drop_after
                if cut is None:
                    yield payload
                    return
                yield payload[:cut]
                if self.stall_after is not None:
                    await asyncio.sleep(3600)
                raise ConnectionResetError("dropped")

            return StreamingResponse(
                gen(), status_code=self.chat_status, media_type=self.chat_content_type
            )

        return Starlette(
            routes=[
                Route("/ping", ping),
                Route("/health", health),
                Route("/v1/chat/completions", chat, methods=["POST"]),
            ]
        )
