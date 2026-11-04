"""T-371 R1 (F4/F7 advocate): reaper heartbeat file, self-watchdog `_exit(70)`, healthcheck."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from harness import Harness
from llmhub.reaper.__main__ import main as reaper_main
from llmhub.reaper.core import (
    STALL_FACTOR,
    WATCHDOG_EXIT_CODE,
    Watchdog,
    heartbeat_age,
    stall_seconds,
    write_heartbeat,
)


def test_stall_seconds_covers_a_full_kill() -> None:
    assert STALL_FACTOR == 3
    assert stall_seconds(15.0, kill_deadline_s=45.0) >= 3 * 15
    # a blocking kill_all (45 s) plus a pass must not trip the watchdog
    assert stall_seconds(15.0, kill_deadline_s=45.0) > 45.0 + 15.0
    assert stall_seconds(5.0, kill_deadline_s=45.0) >= 45.0


def test_write_heartbeat_atomic_and_age(tmp_path: Path) -> None:
    hb = tmp_path / "reaper.hb"
    assert heartbeat_age(hb, time.time()) is None
    write_heartbeat(hb)
    age = heartbeat_age(hb, time.time())
    assert age is not None and 0 <= age < 5
    assert not list(tmp_path.glob("*.tmp"))
    old = time.time() - 600
    os.utime(hb, (old, old))
    age = heartbeat_age(hb, time.time())
    assert age is not None and age >= 599


def test_watchdog_check_fires_once_after_stall() -> None:
    t = [100.0]
    codes: list[int] = []
    wd = Watchdog(45.0, exit_fn=codes.append, monotonic=lambda: t[0])
    wd.beat()
    t[0] += 44.0
    assert wd.check() is False and codes == []
    wd.beat()
    t[0] += 45.5
    assert wd.check() is True and codes == [WATCHDOG_EXIT_CODE]
    assert wd.check() is True and codes == [WATCHDOG_EXIT_CODE]  # fired once


def test_watchdog_beat_writes_heartbeat(tmp_path: Path) -> None:
    hb = tmp_path / "hb"
    wd = Watchdog(45.0, exit_fn=lambda _c: None, heartbeat=hb)
    wd.beat()
    assert hb.exists()


async def test_each_pass_beats(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    r = h.reaper()
    beats: list[float] = []
    stop = asyncio.Event()

    async def counting() -> None:
        loop = asyncio.ensure_future(r.run_loop(15.0, stop, on_pass=lambda: beats.append(1.0)))
        for _ in range(3):
            await h._settle()
            h.clock.advance_to(h.clock.now() + 15.0)
        await h._settle()
        stop.set()
        h.clock.advance_to(h.clock.now() + 15.0)
        await asyncio.wait_for(loop, 5)

    await counting()
    assert len(beats) >= 3


async def test_hung_pass_exits_70(tmp_path: Path) -> None:
    """A pass that never finishes (adapter hangs forever) -> the watchdog thread fires."""
    h = Harness(tmp_path)
    r = h.reaper()
    hang = asyncio.Event()

    async def run_once() -> object:
        await hang.wait()  # never set
        raise AssertionError

    r.run_once = run_once  # type: ignore[method-assign]
    fired = threading.Event()
    codes: list[int] = []

    def exit_fn(code: int) -> None:
        codes.append(code)
        fired.set()

    wd = Watchdog(0.3, exit_fn=exit_fn, check_every_s=0.05)
    wd.start()
    stop = asyncio.Event()
    task = asyncio.ensure_future(r.run_loop(5.0, stop, on_pass=wd.beat))
    try:
        for _ in range(100):
            if fired.is_set():
                break
            await asyncio.sleep(0.02)
    finally:
        wd.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert codes == [WATCHDOG_EXIT_CODE]


def test_healthcheck_cli(tmp_path: Path) -> None:
    hb = tmp_path / "reaper.hb"
    assert reaper_main(["healthcheck", "--heartbeat", str(hb), "--max-age", "60"]) == 1
    write_heartbeat(hb)
    assert reaper_main(["healthcheck", "--heartbeat", str(hb), "--max-age", "60"]) == 0
    old = time.time() - 120
    os.utime(hb, (old, old))
    assert reaper_main(["healthcheck", "--heartbeat", str(hb), "--max-age", "60"]) == 1


def test_loop_cli_accepts_heartbeat_flag() -> None:
    with pytest.raises(SystemExit) as ei:
        reaper_main(["--help"])
    assert ei.value.code == 0


def test_heartbeat_help_mentions_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        reaper_main(["--help"])
    assert "--heartbeat" in capsys.readouterr().out
