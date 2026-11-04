"""One shared poller for all synchronous waiters (one SELECT ... WHERE id IN (...) per tick)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable

from llmhub.clock import Clock
from llmhub.persistence.db import Busy

log = logging.getLogger("llmhub.api.waiter")

TERMINAL = frozenset({"completed", "failed", "cancelled"})
Disconnected = Callable[[], Awaitable[bool]]


class _Waiter:
    __slots__ = ("disconnected", "fut")

    def __init__(self, disconnected: Disconnected | None) -> None:
        self.fut: asyncio.Future[sqlite3.Row | None] = asyncio.get_running_loop().create_future()
        self.disconnected = disconnected


class JobWaiter:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, poll_s: float) -> None:
        self.conn = conn
        self.clock = clock
        self.poll_s = poll_s
        self._waiters: dict[str, list[_Waiter]] = {}
        self._kick = asyncio.Event()

    @property
    def count(self) -> int:
        return sum(len(v) for v in self._waiters.values())

    def notify(self, job_id: str) -> None:
        """A job changed state (local worker, cancel): run a poll tick right away."""
        _ = job_id
        self._kick.set()

    def _row(self, job_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        return row

    async def wait(
        self, job_id: str, until: float, disconnected: Disconnected | None = None
    ) -> sqlite3.Row | None:
        """Terminal row; the current row when `until` passes first; None when the client left
        (the job is NOT cancelled)."""
        row = self._row(job_id)
        if row is None or row["status"] in TERMINAL:
            return row
        w = _Waiter(disconnected)
        self._waiters.setdefault(job_id, []).append(w)
        sleeper = asyncio.ensure_future(self.clock.sleep_until(until))
        try:
            await asyncio.wait({w.fut, sleeper}, return_when=asyncio.FIRST_COMPLETED)
            if w.fut.done():
                return w.fut.result()
            return self._row(job_id)
        finally:
            sleeper.cancel()
            lst = self._waiters.get(job_id, [])
            if w in lst:
                lst.remove(w)
            if not lst:
                self._waiters.pop(job_id, None)
            if not w.fut.done():
                w.fut.cancel()

    async def _tick(self) -> None:
        ids = list(self._waiters)
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        try:
            rows = self.conn.execute(f"SELECT * FROM jobs WHERE id IN ({marks})", ids).fetchall()
        except (Busy, sqlite3.OperationalError):
            return
        by_id = {r["id"]: r for r in rows}
        for job_id in ids:
            row = by_id.get(job_id)
            for w in list(self._waiters.get(job_id, [])):
                if w.fut.done():
                    continue
                if row is None or row["status"] in TERMINAL:
                    w.fut.set_result(row)
                elif w.disconnected is not None and await w.disconnected():
                    w.fut.set_result(None)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self._kick.clear()
            try:
                await self._tick()
            except Exception:  # keep the poller alive
                log.exception("waiter tick failed")
            sleeper = asyncio.ensure_future(self.clock.sleep_until(self.clock.now() + self.poll_s))
            kicker = asyncio.ensure_future(self._kick.wait())
            stopper = asyncio.ensure_future(stop.wait())
            _, pending = await asyncio.wait(
                {sleeper, kicker, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            for p in pending:
                p.cancel()
