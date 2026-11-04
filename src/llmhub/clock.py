"""Injectable clocks: SystemClock for production, FakeClock for deterministic tests."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from llmhub.persistence.db import assert_not_in_tx


class Clock(Protocol):
    def now(self) -> float:
        """Epoch seconds, UTC."""
        ...

    async def sleep_until(self, t: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    async def sleep_until(self, t: float) -> None:
        assert_not_in_tx()
        delay = t - time.time()
        if delay > 0:
            await asyncio.sleep(delay)


class FakeClock:
    """Manual clock. Time moves only through `advance_to`."""

    def __init__(self, start: float = 1_793_750_400.0) -> None:  # 2026-11-04T00:00Z
        self._now = start
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = 0

    def now(self) -> float:
        return self._now

    async def sleep_until(self, t: float) -> None:
        assert_not_in_tx()
        if t <= self._now:
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        self._waiters.append((t, self._seq, fut))
        await fut

    def next_waiter(self) -> float | None:
        times = [t for t, _, f in self._waiters if not f.done()]
        return min(times) if times else None

    def advance_to(self, t: float) -> None:
        if t < self._now:
            raise ValueError("FakeClock cannot go backwards")
        self._now = t
        due = sorted((w for w in self._waiters if w[0] <= t), key=lambda w: (w[0], w[1]))
        self._waiters = [w for w in self._waiters if w[0] > t]
        for _, _, fut in due:
            if not fut.done():
                fut.set_result(None)
