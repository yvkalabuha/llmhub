#!/usr/bin/env python3
"""Fake llama-server for supervisor tests: /health 503 -> 200 after 0.5 s, /env, /argv,
POST echo (any path).

Behaviour switch (non-secret env var): FAKE_LLAMA_MODE=exit exits immediately with code 1;
FAKE_LLAMA_MODE=crash_once (+ FAKE_LLAMA_MARK=<file>): the first run is ready at once and exits
after 1 s, the restarted run reports /health 503 for 1.5 s, then serves normally.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ns, _ = ap.parse_known_args()
    if os.environ.get("FAKE_LLAMA_MODE") == "exit":
        return 1
    t0 = time.monotonic()
    ready_after = 0.5
    if os.environ.get("FAKE_LLAMA_MODE") == "crash_once":
        mark = os.environ["FAKE_LLAMA_MARK"]
        if not os.path.exists(mark):
            open(mark, "w").close()
            ready_after = 0.0
            threading.Timer(1.0, lambda: os._exit(3)).start()
        else:
            ready_after = 1.5

    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: object) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/health":
                ok = time.monotonic() - t0 >= ready_after
                self._send(200 if ok else 503, {"status": "ok" if ok else "loading"})
            elif self.path == "/env":
                self._send(200, sorted(os.environ))
            elif self.path == "/argv":
                self._send(200, sys.argv)
            elif self.path == "/pid":
                self._send(200, os.getpid())
            else:
                self._send(404, {})

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", "0"))
            self._send(
                200,
                {"echo": self.rfile.read(n).decode(), "auth": self.headers.get("Authorization")},
            )

        def log_message(self, format: str, *args: object) -> None:
            pass

    srv = ThreadingHTTPServer((ns.host, ns.port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
