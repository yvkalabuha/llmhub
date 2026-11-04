"""`python -m llmhub.reaper --config PATH [--once | --loop SECONDS]` (default `--loop 15`,
allowed 5..15; longer values are capped at 15 s).

A damaged state.db (F-R2-4) does not stop the reaper: with a KILL file it destroys every
HMAC-verified resource from `list_owned` without the DB and retries opening the DB each pass.

Own DB connection (busy_timeout = `db.reaper_busy_timeout_ms`), own adapters, system clock.
Only the mock provider exists in T-368: its shared store is `<data_dir>/mock_cloud.db`
(override: env `LLMHUB_MOCK_STORE`). Real providers arrive with T-372.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from llmhub.clock import SystemClock
from llmhub.configuration.loader import ConfigError, load_config, load_label_secret
from llmhub.configuration.models import Config
from llmhub.killswitch import check_kill_file, emergency_kill_pass
from llmhub.provider_adapters.base import ProviderAdapter
from llmhub.provider_adapters.mock import MockProvider
from llmhub.provider_adapters.mock_store import MockStore
from llmhub.reaper.core import Reaper, is_corrupt, open_reaper_db

MAX_LOOP_S = 15.0
MIN_LOOP_S = 5.0
EMERGENCY_DEADLINE_S = 45.0


def build_adapters(names: Sequence[str], data_dir: Path, clock: SystemClock) -> dict[str, ProviderAdapter]:
    adapters: dict[str, ProviderAdapter] = {}
    for name in names:
        if name != "mock":
            raise ConfigError(f"provider {name!r} has no adapter yet (T-372)")
        store = os.environ.get("LLMHUB_MOCK_STORE") or str(data_dir / "mock_cloud.db")
        adapters[name] = MockProvider(MockStore(store), clock)
    return adapters


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
    cfg = load_config(Path(args.config))
    secret = load_label_secret()
    clock = SystemClock()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = cfg.data_dir / "state.db"
    adapters = build_adapters(cfg.providers.enabled, cfg.data_dir, clock)
    interval = min(float(args.loop), MAX_LOOP_S)
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
    await reaper.run_loop(interval)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llmhub.reaper")
    ap.add_argument("--config", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", type=float, default=MAX_LOOP_S)
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
