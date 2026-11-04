"""`python -m llmhub.reaper --config PATH [--once | --loop SECONDS]` (default `--loop 15`).

Own DB connection (busy_timeout = `db.reaper_busy_timeout_ms`), own adapters, system clock.
Only the mock provider exists in T-368: its shared store is `<data_dir>/mock_cloud.db`
(override: env `LLMHUB_MOCK_STORE`). Real providers arrive with T-372.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from llmhub.clock import SystemClock
from llmhub.configuration.loader import ConfigError, load_config, load_label_secret
from llmhub.persistence.db import connect, migrate
from llmhub.provider_adapters.base import ProviderAdapter
from llmhub.provider_adapters.mock import MockProvider
from llmhub.provider_adapters.mock_store import MockStore
from llmhub.reaper.core import Reaper

MAX_LOOP_S = 15.0


def build_adapters(names: Sequence[str], data_dir: Path, clock: SystemClock) -> dict[str, ProviderAdapter]:
    adapters: dict[str, ProviderAdapter] = {}
    for name in names:
        if name != "mock":
            raise ConfigError(f"provider {name!r} has no adapter yet (T-372)")
        store = os.environ.get("LLMHUB_MOCK_STORE") or str(data_dir / "mock_cloud.db")
        adapters[name] = MockProvider(MockStore(store), clock)
    return adapters


async def _main(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    secret = load_label_secret()
    clock = SystemClock()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(cfg.data_dir / "state.db", busy_timeout_ms=cfg.db.reaper_busy_timeout_ms)
    migrate(conn)
    reaper = Reaper(conn, cfg, build_adapters(cfg.providers.enabled, cfg.data_dir, clock),
                    clock, secret)  # fmt: skip
    if args.once:
        report = await reaper.run_once()
        print(
            f"reaper: destroyed={len(report.sweep.destroyed)} stopped={len(report.stopped)}"
            f" unknown={len(report.sweep.unknown)} kill={report.kill is not None}"
        )
        return 0
    await reaper.run_loop(min(float(args.loop), MAX_LOOP_S))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llmhub.reaper")
    ap.add_argument("--config", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", type=float, default=MAX_LOOP_S)
    args = ap.parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
