"""`python -m llmhub.reaper --config PATH [--once | --loop SECONDS] [--heartbeat FILE]`
(default `--loop 15`, allowed 5..15; longer values are capped at 15 s).
`python -m llmhub.reaper healthcheck --heartbeat FILE --max-age S` -> 0 fresh, 1 stale/none.

`--loop` runs a self-watchdog (T-371): no finished pass for `stall_seconds(loop)` -> the
process exits with 70 and Docker (`--restart unless-stopped`) starts a new one. Each pass
rewrites `--heartbeat` (the container healthcheck reads its age).

A damaged state.db (F-R2-4) does not stop the reaper: with a KILL file it destroys every
HMAC-verified resource from `list_owned` without the DB and retries opening the DB each pass.

Own DB connection (busy_timeout = `db.reaper_busy_timeout_ms`), own adapters (the shared
`provider_adapters.factory`: mock store `<data_dir>/mock_cloud.db`, override env
`LLMHUB_MOCK_STORE`; RunPod key from `LLMHUB_RUNPOD_API_KEY_FILE`), system clock.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from llmhub.clock import SystemClock
from llmhub.configuration.loader import ConfigError, load_config, load_label_secret
from llmhub.configuration.models import Config
from llmhub.killswitch import check_kill_file, emergency_kill_pass
from llmhub.provider_adapters.base import ProviderAdapter
from llmhub.provider_adapters.factory import build_provider_adapters
from llmhub.reaper.core import (
    Reaper,
    Watchdog,
    heartbeat_age,
    is_corrupt,
    open_reaper_db,
    stall_seconds,
)

MAX_LOOP_S = 15.0
MIN_LOOP_S = 5.0
EMERGENCY_DEADLINE_S = 45.0


def build_adapters(cfg: Config, clock: SystemClock) -> dict[str, ProviderAdapter]:
    return build_provider_adapters(cfg, os.environ, clock)


async def _emergency_once(reaper_cfg: Config, adapters: dict[str, ProviderAdapter],
                          secret: bytes, clock: SystemClock, exc: BaseException) -> int:
    """`--once` with a damaged DB: one emergency kill (polled up to 45 s) and exit 1."""
    print(f"reaper: state.db unusable ({exc}); emergency mode", file=sys.stderr)
    if not check_kill_file(reaper_cfg.data_dir):
        return 1
    deadline = clock.now() + EMERGENCY_DEADLINE_S
    while True:
        remaining = await emergency_kill_pass(adapters, secret)
        if not remaining or clock.now() >= deadline:
            break
        await clock.sleep_until(clock.now() + 2.0)
    print(f"reaper: emergency kill, remaining={len(remaining)}", file=sys.stderr)
    return 1


async def _main(args: argparse.Namespace) -> int:
    os.umask(0o027)  # state.db-wal/-shm created by the reaper stay 0640 (T-371 §2.3)
    cfg = load_config(Path(args.config))
    secret = load_label_secret()
    clock = SystemClock()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = cfg.data_dir / "state.db"
    adapters = build_adapters(cfg, clock)
    interval = min(float(args.loop), MAX_LOOP_S)
    watchdog: Watchdog | None = None
    if not args.once:
        hb = Path(args.heartbeat) if args.heartbeat else None
        watchdog = Watchdog(stall_seconds(interval, kill_deadline_s=EMERGENCY_DEADLINE_S),
                            heartbeat=hb)  # fmt: skip
        watchdog.start()
    try:
        return await _run(args, cfg, secret, clock, db_path, adapters, interval, watchdog)
    finally:
        if watchdog is not None:
            watchdog.stop()


async def _run(
    args: argparse.Namespace,
    cfg: Config,
    secret: bytes,
    clock: SystemClock,
    db_path: Path,
    adapters: dict[str, ProviderAdapter],
    interval: float,
    watchdog: Watchdog | None,
) -> int:
    while True:  # F-R2-4: a damaged DB never stops the KILL file from being honoured
        try:
            conn = open_reaper_db(db_path, cfg)
            break
        except sqlite3.DatabaseError as exc:
            if not is_corrupt(exc) and not isinstance(exc, sqlite3.OperationalError):
                raise
            if args.once:
                return await _emergency_once(cfg, adapters, secret, clock, exc)
            started = clock.now()
            print(f"reaper: state.db unusable ({exc}); emergency mode", file=sys.stderr)
            if check_kill_file(cfg.data_dir):
                await emergency_kill_pass(adapters, secret)
            if watchdog is not None:
                watchdog.beat()
            await clock.sleep_until(started + interval)
    reaper = Reaper(conn, cfg, adapters, clock, secret, db_path=db_path)
    if args.once:
        try:
            report = await reaper.run_once()
        except sqlite3.DatabaseError as exc:
            if not is_corrupt(exc):
                raise
            return await _emergency_once(cfg, adapters, secret, clock, exc)
        print(
            f"reaper: destroyed={len(report.sweep.destroyed)} stopped={len(report.stopped)}"
            f" unknown={len(report.sweep.unknown)} kill={report.kill is not None}"
        )
        return 0
    await reaper.run_loop(interval, on_pass=None if watchdog is None else watchdog.beat)
    return 0


def _healthcheck(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m llmhub.reaper healthcheck")
    ap.add_argument("--heartbeat", required=True)
    ap.add_argument("--max-age", type=float, default=90.0)
    args = ap.parse_args(argv)
    age = heartbeat_age(Path(args.heartbeat), time.time())
    if age is None or age > args.max_age:
        print("reaper: heartbeat " + ("missing" if age is None else f"stale ({age:.0f}s)"),
              file=sys.stderr)  # fmt: skip
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["healthcheck"]:
        return _healthcheck(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m llmhub.reaper")
    ap.add_argument("--config", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", type=float, default=MAX_LOOP_S)
    ap.add_argument("--heartbeat", default=None,
                    help="file rewritten after every --loop pass (container healthcheck)")
    args = ap.parse_args(argv)
    if args.loop < MIN_LOOP_S:  # L3: a tight loop would hammer the provider API
        ap.error(f"--loop must be >= {MIN_LOOP_S:g} seconds")
    try:
        return asyncio.run(_main(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
