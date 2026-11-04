"""`auth_request` validator for the weights nginx (T-370 stream D, plan 2a).

GET /weights/auth  -> 204 | 401 (no credentials) | 403 | 503 (controller DB or validator state
                      busy/broken: fail closed, the pod fails over to HF)
GET /metrics       -> Prometheus text with refusal counters, 127.0.0.1 only.
The controller DB is opened read-only (mode=ro); the validator never writes to it. Logs carry
op_id and verdict only - never the token, the URI query or the client address.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from llmhub.clock import Clock, SystemClock
from llmhub.configuration.loader import ConfigError, load_config, load_weights_secret
from llmhub.configuration.models import Config
from llmhub.weights.acct import AcctState
from llmhub.weights.tokens import Verdict, check_request, split_uri

log = logging.getLogger("llmhub.weights.auth")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_LOCAL = frozenset({"127.0.0.1", "::1"})
# rel -> (file size, sum of sizes of its weight set)
Sizes = Mapping[str, tuple[int, int]]


def requested_bytes(range_header: str | None, size: int) -> int:
    """Bytes a request may cost: no/unsupported Range -> whole file."""
    m = _RANGE.match((range_header or "").strip())
    if m is None or (m.group(1) == "" and m.group(2) == ""):
        return size
    a, b = m.groups()
    if a == "":  # suffix: last N bytes
        return min(int(b), size)
    start = int(a)
    end = size - 1 if b == "" else min(int(b), size - 1)
    return max(0, end - start + 1)


def _open_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.3)
    conn.execute("PRAGMA busy_timeout=300")
    return conn


def build_auth_app(
    db_path: Path,
    secret: bytes,
    allowed: frozenset[str],
    clock: Clock,
    *,
    acct: AcctState,
    sizes: Sizes,
    volume_cap_factor: float = 1.2,
) -> Starlette:
    counters: Counter[str] = Counter()
    lock = threading.Lock()

    def state_of(op_id: str) -> str | None:
        conn = _open_ro(db_path)
        try:
            row = conn.execute("SELECT state FROM instances WHERE op_id = ?", (op_id,)).fetchone()
        finally:
            conn.close()
        return None if row is None else str(row[0])

    def finish(verdict: Verdict, op: str | None, status: int) -> Response:
        with lock:
            counters[verdict.value] += 1
        log.info("op=%s verdict=%s", op or "-", verdict.value)
        return Response(status_code=status)

    def auth(request: Request) -> Response:
        h = request.headers
        uri = h.get("x-original-uri", "")
        try:
            verdict, op = check_request(
                secret, h.get("authorization"), uri, clock.now(), state_of, allowed
            )
        except sqlite3.Error:
            return finish(Verdict.DB_BUSY, None, 503)
        if verdict is Verdict.MISSING:
            return finish(verdict, op, 401)
        if verdict is not Verdict.OK or op is None:
            return finish(verdict, op, 403)
        parts = split_uri(uri)
        size, total = sizes.get(parts[1], (0, 0)) if parts else (0, 0)
        try:
            if not acct.bind_ip(op, h.get("x-real-ip", "")):
                return finish(Verdict.IP_MISMATCH, op, 403)
            if size <= 0:
                return finish(Verdict.PATH, op, 403)
            # If-Range (SEC-1): on a validator mismatch nginx ignores Range and sends 200 with
            # the whole file, so any If-Range is charged as the whole file.
            if h.get("x-if-range", "").strip():
                need = size
            else:
                need = requested_bytes(h.get("x-range"), size)
            over = acct.served(op) + need > volume_cap_factor * total
        except sqlite3.Error:  # validator state unavailable: fail closed (SEC-8)
            return finish(Verdict.DB_BUSY, op, 503)
        if over:
            return finish(Verdict.VOLUME_CAP, op, 403)
        return finish(Verdict.OK, op, 204)

    def metrics(request: Request) -> Response:
        if request.client is None or request.client.host not in _LOCAL:
            return Response(status_code=403)
        with lock:
            snap = dict(counters)
        lines = ["# TYPE llmhub_weights_auth_total counter"]
        lines += [
            f'llmhub_weights_auth_total{{verdict="{v}"}} {n}' for v, n in sorted(snap.items())
        ]
        return PlainTextResponse("\n".join(lines) + "\n")

    return Starlette(routes=[Route("/weights/auth", auth), Route("/metrics", metrics)])


def tables_from_config(cfg: Config) -> tuple[frozenset[str], dict[str, tuple[int, int]]]:
    """Allowed `rel` paths and their sizes from the self_hosted sources of all weight sets."""
    sizes: dict[str, tuple[int, int]] = {}
    for src in cfg.weights.sources:
        if src.kind != "self_hosted":
            continue
        for wset in cfg.weights.sets.values():
            for f in wset.files:
                rel = src.paths.get(f.id)
                if rel is not None:
                    prev = sizes.get(rel, (0, 0))
                    sizes[rel] = (f.size, max(prev[1], wset.total_size))
    return frozenset(sizes), sizes


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    run: Callable[..., object] | None = None,
) -> int:
    import os

    ap = argparse.ArgumentParser(prog="python -m llmhub.weights.auth_app")
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    environ = os.environ if env is None else env
    # SEC-12: op/verdict lines go to stderr (journald under systemd); never token or IP
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")  # fmt: skip
    try:
        cfg = load_config(Path(args.config), environ)
        secret = load_weights_secret(environ)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    allowed, sizes = tables_from_config(cfg)
    host, _, port = cfg.weights.auth_listen.rpartition(":")
    host = host.removeprefix("[").removesuffix("]")  # loopback only (config validator, SEC-9)
    app = build_auth_app(
        cfg.data_dir / "state.db",
        secret,
        allowed,
        SystemClock(),
        acct=AcctState(cfg.weights.auth_state_dir, cfg.weights.acct_log),
        sizes=sizes,
        volume_cap_factor=cfg.weights.volume_cap_factor,
    )
    if run is None:
        import uvicorn

        run = uvicorn.run
    run(app, host=host, port=int(port), log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
