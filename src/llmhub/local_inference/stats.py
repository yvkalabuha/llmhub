"""Latency EWMA and availability / external-busy state of one local backend (T-369 stream H)."""

from __future__ import annotations

from dataclasses import dataclass

from llmhub.local_inference.client import LocalChatResult, ProbeResult


class Ewma:
    def __init__(self, alpha: float, initial: float) -> None:
        self.alpha = alpha
        self.value = initial

    def update(self, x: float) -> float:
        self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


@dataclass
class BackendStats:
    pp: Ewma
    tg: Ewma
    available: bool = True
    failures: int = 0
    busy_external: bool = False
    last_probe_at: float | None = None

    def on_result(self, r: LocalChatResult) -> None:
        """Speeds from llama-server `timings`; without them only generation speed from latency."""
        if r.prompt_tps is not None and r.prompt_tps > 0:
            self.pp.update(r.prompt_tps)
        if r.gen_tps is not None and r.gen_tps > 0:
            self.tg.update(r.gen_tps)
        elif r.prompt_tps is None and r.tokens_out > 0 and r.latency_s > 0:
            self.tg.update(r.tokens_out / r.latency_s)
        self.failures = 0
        self.available = True

    def on_probe(self, p: ProbeResult, ours_running: bool, unavailable_after: int) -> None:
        if p.ok:
            self.failures = 0
            self.available = True
            self.busy_external = p.busy_slots > 0 and not ours_running
        else:
            self.failures += 1
            self.busy_external = False
            if self.failures >= unavailable_after:
                self.available = False
