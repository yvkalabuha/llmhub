"""Weights-server probe (T-371 S10w): one pinned-TLS Range GET of 1 MiB with a fresh token.

    docker exec -i llmhub-weights-auth python -m llmhub.tools.weights_probe \
        --config /etc/llmhub/conf/config.yaml --op <op_id> [--expect-sha256 HEX] [--negatives]

Runs where the weights secret is already mounted (`LLMHUB_WEIGHTS_SECRET_FILE`). The op must
be a live instance (e.g. a Mock pool held in BOOTING). Before the request the host
MemAvailable is checked against `weights.min_mem_available_mib` (abort, exit 3). The token is
never printed: the report (stdout, JSON) has statuses, byte counts and sha256 only.
`--negatives`: a token of another op -> 403, an expired token -> 403, no token -> 401.
Exit 0 all ok, 1 a check failed, 2 config/arguments, 3 low memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import ssl
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from llmhub.configuration.loader import ConfigError, load_config, load_weights_secret
from llmhub.configuration.models import Config
from llmhub.weights.auth_app import mem_available_mib
from llmhub.weights.tokens import LOADING_STATES, issue_token

PROBE_BYTES = 1024 * 1024
OTHER_OP = "0" * 12
EXIT_LOW_MEMORY = 3


@dataclass(frozen=True)
class Outcome:
    status: int | None
    received: int
    sha256: str | None
    error: str | None = None


def target(cfg: Config, op: str, rel: str | None) -> tuple[str, Path]:
    """(URL of the file for `op`, pinned cert) from the first self_hosted source."""
    for src in cfg.weights.sources:
        if src.kind != "self_hosted" or src.ca_pem_file is None:
            continue
        path = rel or next(iter(src.paths.values()), None)
        if path is None:
            break
        base = src.base_url.replace("{op_id}", op).rstrip("/")
        return f"{base}/{path}", src.ca_pem_file
    raise ConfigError("weights.sources: no self_hosted source with paths and ca_pem_file")


def fetch(
    url: str, token: str | None, ca_pem: str, nbytes: int, *,
    timeout: float = 30.0, transport: httpx.BaseTransport | None = None,
) -> Outcome:  # fmt: skip
    """GET Range bytes=0..nbytes-1 with TLS pinned to `ca_pem` (no system CAs)."""
    headers = {"Range": f"bytes=0-{nbytes - 1}"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    ctx = ssl.create_default_context(cadata=ca_pem)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        with httpx.Client(verify=ctx, timeout=timeout, transport=transport) as c:
            r = c.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return Outcome(None, 0, None, type(exc).__name__)
    body = r.content if r.status_code in (200, 206) else b""
    return Outcome(r.status_code, len(body), hashlib.sha256(body).hexdigest() if body else None)


def run_probe(
    cfg: Config, secret: bytes, op: str, *, rel: str | None = None, nbytes: int = PROBE_BYTES,
    expect_sha256: str | None = None, negatives: bool = False,
    transport: httpx.BaseTransport | None = None, now: float | None = None,
    meminfo: Path | None = None,
) -> tuple[int, dict[str, Any]]:  # fmt: skip
    t = time.time() if now is None else now
    report: dict[str, Any] = {"op": op, "checks": []}

    def add(name: str, ok: bool, **kw: Any) -> None:
        report["checks"].append({"name": name, "ok": ok, **kw})

    limit = cfg.weights.min_mem_available_mib
    avail = mem_available_mib(meminfo) if meminfo is not None else mem_available_mib()
    report["mem_available_mib"] = avail
    if limit > 0 and avail is not None and avail < limit:
        add("mem_available", False, threshold_mib=limit)
        return EXIT_LOW_MEMORY, report
    url, ca_file = target(cfg, op, rel)
    ca = Path(ca_file).read_text(encoding="utf-8")
    exp = int(t) + 600
    got = fetch(url, issue_token(secret, op, exp), ca, nbytes, transport=transport)
    ok = got.status == 206 and got.received == nbytes
    if expect_sha256 is not None:
        ok = ok and got.sha256 == expect_sha256
    add("range_206", ok, status=got.status, received=got.received, sha256=got.sha256,
        error=got.error)  # fmt: skip
    if negatives:
        other_url, _ = target(cfg, OTHER_OP, rel)
        o = fetch(other_url, issue_token(secret, OTHER_OP, exp), ca, 16, transport=transport)
        add("other_op_403", o.status == 403, status=o.status)
        old = fetch(url, issue_token(secret, op, int(t) - 60), ca, 16, transport=transport)
        add("expired_token_403", old.status == 403, status=old.status)
        none = fetch(url, None, ca, 16, transport=transport)
        add("no_token_401", none.status == 401, status=none.status)
    passed = all(c["ok"] for c in report["checks"])
    report["ok"] = passed
    return (0 if passed else 1), report


def latest_loading_op(db_path: Path) -> str | None:
    """`--op auto`: op_id of the newest instance in a loading state (read-only DB access)."""
    states = sorted(LOADING_STATES)
    marks = ",".join("?" for _ in states)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute(
            f"SELECT op_id FROM instances WHERE state IN ({marks}) ORDER BY created_at DESC LIMIT 1",
            states,
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llmhub.tools.weights_probe")
    ap.add_argument("--config", required=True)
    ap.add_argument("--op", required=True,
                    help="op_id of a loading instance (12 hex) or 'auto' (newest one)")
    ap.add_argument("--rel", default=None, help="file path under the source (default: first)")
    ap.add_argument("--bytes", type=int, default=PROBE_BYTES)
    ap.add_argument("--expect-sha256", default=None)
    ap.add_argument("--negatives", action="store_true")
    a = ap.parse_args(argv)
    environ = os.environ if env is None else env
    try:
        cfg = load_config(Path(a.config), environ)
        secret = load_weights_secret(environ)
        op = a.op
        if op == "auto":
            found = latest_loading_op(cfg.data_dir / "state.db")
            if found is None:
                print("error: no instance in PROVISIONING/BOOTING/LOADING_MODEL", file=sys.stderr)
                return 2
            op = found
        code, report = run_probe(cfg, secret, op, rel=a.rel, nbytes=a.bytes,
                                 expect_sha256=a.expect_sha256, negatives=a.negatives)  # fmt: skip
    except (ConfigError, OSError, sqlite3.Error) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
