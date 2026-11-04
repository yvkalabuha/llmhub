"""Fakes for the pod downloader: shared fake clock, in-process weights server, TLS server."""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self

import httpx


class FakeClock:
    """One clock for SpeedMonitor, Fetcher and the fake server; nothing sleeps for real."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def advance(self, dt: float) -> None:
        with self._lock:
            self._t += dt

    def sleep(self, dt: float) -> None:
        self.advance(dt)


@dataclass
class Request:
    host: str
    path: str
    range: str | None
    authorization: str | None


class _Stream(httpx.SyncByteStream):
    def __init__(self, server: FakeWeightsServer, data: bytes, drop_after: int | None) -> None:
        self._s, self._d, self._drop = server, data, drop_after

    def __iter__(self) -> Iterator[bytes]:
        sent = 0
        step = self._s.chunk
        for i in range(0, len(self._d), step):
            part = self._d[i : i + step]
            if self._drop is not None and sent + len(part) > self._drop:
                part = part[: max(0, self._drop - sent)]
                if part:
                    yield part
                raise httpx.ReadError("connection dropped")
            if self._s.throttle_bps:
                self._s.clock.advance(len(part) / self._s.throttle_bps)
            sent += len(part)
            yield part


@dataclass
class FakeWeightsServer(httpx.BaseTransport):
    """In-process httpx transport. Keys of `files` are URL paths; any host is served."""

    files: dict[str, bytes]
    clock: FakeClock
    range_support: bool = True
    ignore_range: bool = False
    status_for: dict[str, int] = field(default_factory=dict)
    fail_first_n: dict[str, int] = field(default_factory=dict)
    throttle_bps: float | None = None
    drop_after_bytes: int | None = None
    drop_times: int = 1
    corrupt: set[str] = field(default_factory=set)
    redirect_to: dict[str, str] = field(default_factory=dict)
    redirect_status_after: dict[str, int] = field(default_factory=dict)
    chunk: int = 4096
    log: list[Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, int] = {}
        self._drops = 0

    def requests_for(self, path: str) -> list[Request]:
        return [r for r in self.log if r.path == path]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        rng = request.headers.get("range")
        with self._lock:
            self.log.append(
                Request(request.url.host, path, rng, request.headers.get("authorization"))
            )
            if path in self.fail_first_n:
                n = self._fails.get(path, 0)
                if n < self.fail_first_n[path]:
                    self._fails[path] = n + 1
                    return httpx.Response(503)
            drop = None
            if self.drop_after_bytes is not None and self._drops < self.drop_times:
                drop = self.drop_after_bytes
                self._drops += 1
        if path in self.status_for:
            return httpx.Response(self.status_for[path])
        if path in self.redirect_to:
            return httpx.Response(302, headers={"location": self.redirect_to[path]})
        if path not in self.files:
            return httpx.Response(404)
        data = self.files[path]
        if path in self.corrupt:
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        total = len(data)
        m = re.match(r"^bytes=(\d+)-(\d*)$", rng or "")
        if m and self.range_support and not self.ignore_range:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else total - 1
            if a >= total:
                return httpx.Response(416, headers={"content-range": f"bytes */{total}"})
            b = min(b, total - 1)
            body = data[a : b + 1]
            return httpx.Response(
                206,
                headers={
                    "content-range": f"bytes {a}-{b}/{total}",
                    "content-length": str(len(body)),
                },
                stream=_Stream(self, body, drop),
            )
        return httpx.Response(
            200, headers={"content-length": str(total)}, stream=_Stream(self, data, drop)
        )


def make_plan_json(
    files: dict[str, bytes],
    sources: list[dict[str, Any]],
    *,
    deadline_unix: float | None = None,
    **over: Any,
) -> str:
    """files: id -> content. Each source dict needs name, base_url, paths; rest defaults."""
    plan: dict[str, Any] = {
        "version": 1,
        "files": [
            {"id": k, "size": len(v), "sha256": hashlib.sha256(v).hexdigest()}
            for k, v in files.items()
        ],
        "sources": [{"ca_pem": None, "auth": "none", "min_speed_mb_s": 0, **s} for s in sources],
        "speed_grace_s": 0.3,
        "speed_window_s": 0.2,
        "retries_per_source": 2,
    }
    if deadline_unix is not None:
        plan["deadline_unix"] = deadline_unix
    plan.update(over)
    return json.dumps(plan)


class TlsServer:
    """Real threaded HTTPS server (loopback) serving one body for any GET."""

    def __init__(self, cert_pem: str, key_pem: str, body: bytes) -> None:
        self.body = body
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._tmp = tempfile.TemporaryDirectory()
        cert = Path(self._tmp.name) / "c.pem"
        key = Path(self._tmp.name) / "k.pem"
        cert.write_text(cert_pem)
        key.write_text(key_pem)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.socket = ctx.wrap_socket(self._srv.socket, server_side=True)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *a: object) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._tmp.cleanup()
