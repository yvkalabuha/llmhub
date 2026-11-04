"""Sliding-window download speed monitor driven by an injected clock."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable


class SpeedMonitor:
    def __init__(
        self, clock: Callable[[], float], grace_s: float, window_s: float, min_bps: float
    ) -> None:
        self._clock = clock
        self._grace = grace_s
        self._window = window_s
        self._min = min_bps
        self._t0 = clock()
        self._events: deque[tuple[float, int]] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def add(self, nbytes: int) -> None:
        now = self._clock()
        self._events.append((now, nbytes))
        self._prune(now)

    def bps(self) -> float:
        now = self._clock()
        self._prune(now)
        span = min(self._window, now - self._t0)
        if span <= 0:
            return 0.0
        return sum(n for _, n in self._events) / span

    def too_slow(self) -> bool:
        if self._min <= 0:
            return False
        if self._clock() - self._t0 < self._grace:
            return False
        return self.bps() < self._min
