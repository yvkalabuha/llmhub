"""Pure ETA functions for the single local CPU slot (T-369 stream H)."""

from __future__ import annotations

from collections.abc import Sequence

from llmhub.configuration.api_models import LocalConfig
from llmhub.local_inference.stats import BackendStats


def work_s(tokens_in: int, max_tokens: int, pp: float, tg: float) -> float:
    """Seconds of CPU work: prompt processing plus the worst-case generation."""
    return max(0, tokens_in) / pp + max(0, max_tokens) / tg


def local_eta_s(
    *,
    ahead: Sequence[tuple[int, int]],
    running_left_s: float,
    own: tuple[int, int],
    stats: BackendStats,
    cfg: LocalConfig,
) -> float:
    """running_left + sum(work(ahead)) + work(own) + external-busy penalty."""
    pp, tg = stats.pp.value, stats.tg.value
    total = max(0.0, running_left_s)
    total += sum(work_s(i, o, pp, tg) for i, o in ahead)
    total += work_s(own[0], own[1], pp, tg)
    if stats.busy_external:
        total += cfg.external_busy_penalty_s
    return total
