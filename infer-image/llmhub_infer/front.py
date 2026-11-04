"""Pod front: API-key check, /health with weights progress, reverse proxy to llama-server."""

from __future__ import annotations

import hmac
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

MAX_BODY = 8 * 1024 * 1024
# the only llama-server paths reachable through the front (after normalization)
ALLOWED_PATHS = frozenset(
    {"/v1/chat/completions", "/v1/completions", "/v1/embeddings", "/v1/models", "/props"}
)
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "authorization",
    }
)


@dataclass
class PodState:
    """Shared pod status, written by the supervisor and read by the front.

    phase: ``loading`` (weights) -> ``llama_load`` (llama-server starting) -> ``ready``;
    ``failed`` is terminal (the process stays alive and answers 500).
    ``restarting``: llama-server exited once and its single restart is in progress (phase is
    ``llama_load``); cleared when llama is ready again. The proxy then answers 503
    ``{"error": "restarting"}`` instead of ``{"error": "not ready"}``.
    """

    source_names: tuple[str, ...] = ()
    phase: str = "loading"
    reason: str | None = None
    causes: tuple[str, ...] = ()  # per-source failover reasons of a failed download (R2-1)
    progress: dict[str, Any] = field(default_factory=dict)
    restarting: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_progress(self, p: object) -> None:
        """Accept a downloader ``Progress`` (duck-typed: phase/file/source/done/total/bps/...)."""
        snap = {
            "phase": getattr(p, "phase", "loading"),
            "file": getattr(p, "file", None),
            "source": getattr(p, "source", None),
            "done": int(getattr(p, "done", 0) or 0),
            "total": int(getattr(p, "total", 0) or 0),
            "bps": float(getattr(p, "bps", 0.0) or 0.0),
            "failovers": list(getattr(p, "failovers", []) or []),
        }
        if hasattr(p, "eta_s") and hasattr(p, "sources_left"):  # downloader-computed values win
            eta = getattr(p, "eta_s")  # noqa: B009
            snap["eta_s"] = None if eta is None else round(float(eta), 1)
            snap["sources_left"] = int(getattr(p, "sources_left"))  # noqa: B009
        with self._lock:
            self.progress = snap

    def set_phase(self, phase: str) -> None:
        with self._lock:
            if self.phase != "failed":
                self.phase = phase
                if phase == "ready":
                    self.restarting = False

    def set_restarting(self) -> None:
        with self._lock:
            if self.phase != "failed":
                self.restarting = True
                self.phase = "llama_load"

    def is_restarting(self) -> bool:
        with self._lock:
            return self.restarting and self.phase != "failed"

    def fail(self, reason: str, causes: tuple[str, ...] = ()) -> None:
        with self._lock:
            self.phase = "failed"
            self.reason = reason
            self.causes = tuple(causes)

    def failed_body(self) -> dict[str, Any]:
        with self._lock:
            body: dict[str, Any] = {"status": "failed", "reason": self.reason}
            if self.causes:
                body["causes"] = list(self.causes)
            return body

    def snapshot(self) -> tuple[str, str | None, dict[str, Any]]:
        with self._lock:
            return self.phase, self.reason, dict(self.progress)

    def sources_left(self, source: str | None) -> int:
        names = self.source_names
        if source in names:
            return len(names) - 1 - names.index(source)  # type: ignore[arg-type]
        return len(names)


def _loading_body(state: PodState, progress: dict[str, Any]) -> dict[str, Any]:
    bps = float(progress.get("bps") or 0.0)
    done = int(progress.get("done") or 0)
    total = int(progress.get("total") or 0)
    eta = round((total - done) / bps, 1) if bps > 0 and total > done else None
    if "eta_s" in progress:
        eta = progress["eta_s"]
    left = progress.get("sources_left", state.sources_left(progress.get("source")))
    return {
        "status": "loading",
        "phase": progress.get("phase", "loading"),
        "file": progress.get("file"),
        "source": progress.get("source"),
        "done": done,
        "total": total,
        "mb_s": round(bps / 1e6, 2),
        "failovers": progress.get("failovers", []),
        "eta_s": eta,
        "sources_left": left,
    }


def build_app(
    state: PodState,
    api_key: str,
    upstream: str = "http://127.0.0.1:8081",
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    key_bytes = api_key.encode()
    clients: dict[str, httpx.AsyncClient] = {}

    def client() -> httpx.AsyncClient:
        if "c" not in clients:
            clients["c"] = httpx.AsyncClient(
                base_url=upstream,
                transport=upstream_transport,
                timeout=httpx.Timeout(10.0, read=None),
            )
        return clients["c"]

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        return hmac.compare_digest(token.strip().encode(), key_bytes)

    def unauthorized() -> Response:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async def ping(request: Request) -> Response:
        return Response(status_code=204)

    async def health(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        phase, _reason, progress = state.snapshot()
        if phase == "failed":
            return JSONResponse(state.failed_body(), status_code=500)
        if phase == "loading":
            return JSONResponse(_loading_body(state, progress), status_code=503)
        try:
            r = await client().get("/health", timeout=3.0)
            alive = r.status_code == 200
        except httpx.HTTPError:
            alive = False
        if alive:
            state.set_phase("ready")
            return JSONResponse({"status": "ok"})
        body = _loading_body(state, progress)
        body["phase"] = "llama_load"
        if state.is_restarting():
            body["restarting"] = True
        return JSONResponse(body, status_code=503)

    async def proxy(request: Request) -> Response:
        if not authorized(request):
            return unauthorized()
        path = _safe_path(request)
        if path is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if state.snapshot()[0] != "ready":
            if state.is_restarting():
                return JSONResponse({"error": "restarting"}, status_code=503)
            return JSONResponse({"error": "not ready"}, status_code=503)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY:
            return JSONResponse({"error": "body too large"}, status_code=413)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                return JSONResponse({"error": "body too large"}, status_code=413)
            chunks.append(chunk)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        req = client().build_request(
            request.method,
            path,
            params=request.query_params,
            headers=headers,
            content=b"".join(chunks),
        )
        try:
            resp = await client().send(req, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"error": "upstream unavailable"}, status_code=502)
        out_headers = _clean(resp.headers)

        async def body() -> AsyncIterator[bytes]:
            try:
                async for part in resp.aiter_raw():
                    yield part
            finally:
                await resp.aclose()

        return StreamingResponse(body(), status_code=resp.status_code, headers=out_headers)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        if "c" in clients:
            await clients["c"].aclose()

    methods = ["GET", "POST", "OPTIONS"]
    return Starlette(
        routes=[
            Route("/ping", ping, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/props", proxy, methods=methods),
            Route("/v1/{rest:path}", proxy, methods=methods),
        ],
        lifespan=lifespan,
    )


def _safe_path(request: Request) -> str | None:
    """The normalized request path if it is an allowed llama-server path, else None.

    Works on the raw (still percent-encoded) target: encoded slashes/backslashes, a backslash,
    `.`/`..` or empty segments are refused, so the upstream path is exactly an allow-listed one.
    """
    raw = request.scope.get("raw_path")
    target = raw.decode("latin-1") if isinstance(raw, bytes) and raw else request.url.path
    target = target.split("?", 1)[0]
    low = target.lower()
    if "%2f" in low or "%5c" in low or "\\" in target or "%25" in low:
        return None
    path = unquote(target)
    if "\\" in path or not path.startswith("/"):
        return None
    if any(seg in ("", ".", "..") for seg in path[1:].split("/")):
        return None
    return path if path in ALLOWED_PATHS else None


def _clean(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}
