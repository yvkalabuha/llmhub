"""Pre-launch cost estimate E_launch = r * (T_cold_p90 + sum T_work + T_idle) + traffic + storage."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

from llmhub.cost_tracker.txutil import join_or_open, state_get, state_set


@dataclass(frozen=True)
class JobLoad:
    job_id: str
    tokens_in: int
    max_tokens: int  # generation is estimated by max_tokens (upper bound)


@dataclass(frozen=True)
class LaunchEstimate:
    usd: float
    t_cold_s: float
    work_s: float
    idle_s: float
    per_job: dict[str, float] = field(default_factory=dict)  # share_j (see plan section 6)
    price_usd_h: float = 0.0  # extension: effective rate incl. disk, for balance checks


def t_cold_p90(conn: sqlite3.Connection, pool: str, default_s: float, n: int = 20) -> float:
    """Nearest-rank p90 of created_at -> ready_at over the last n starts; default below 3."""
    rows = conn.execute(
        "SELECT ready_at - created_at AS d FROM instances"
        " WHERE pool=? AND ready_at IS NOT NULL ORDER BY created_at DESC LIMIT ?",
        (pool, n),
    ).fetchall()
    if len(rows) < 3:
        return default_s
    durations = sorted(float(r["d"]) for r in rows)
    return durations[math.ceil(0.9 * len(durations)) - 1]


def work_seconds(tokens_in: int, max_tokens: int, pp: float, tg: float) -> float:
    return tokens_in / pp + max_tokens / tg


def estimate_launch(
    price_usd_h: float,
    jobs: Sequence[JobLoad],
    t_cold_s: float,
    idle_s: float,
    pp: float,
    tg: float,
    traffic_usd: float = 0.0,
    storage_usd: float = 0.0,
    disk_usd_h: float = 0.0,
) -> LaunchEstimate:
    rate_h = price_usd_h + disk_usd_h
    r = rate_h / 3600.0
    works = {j.job_id: work_seconds(j.tokens_in, j.max_tokens, pp, tg) for j in jobs}
    total_work = sum(works.values())
    n = max(1, len(jobs))
    usd = r * (t_cold_s + total_work + idle_s) + traffic_usd + storage_usd
    shared = r * (t_cold_s + idle_s) / n
    per_job = {jid: r * w + shared for jid, w in works.items()}
    return LaunchEstimate(
        usd=usd, t_cold_s=t_cold_s, work_s=total_work, idle_s=idle_s, per_job=per_job,
        price_usd_h=rate_h,
    )  # fmt: skip


def get_speeds(conn: sqlite3.Connection, pool: str, pp0: float, tg0: float) -> tuple[float, float]:
    """EWMA of observed speeds; the pool profile before the first observation."""
    pp = state_get(conn, f"ewma:{pool}:pp")
    tg = state_get(conn, f"ewma:{pool}:tg")
    return (float(pp) if pp else pp0, float(tg) if tg else tg0)


def update_speed_ewma(
    conn: sqlite3.Connection,
    pool: str,
    pp_obs: float | None,
    tg_obs: float | None,
    alpha: float = 0.3,
    *,
    now: float = 0.0,
    pp0: float | None = None,
    tg0: float | None = None,
) -> None:
    """EWMA (alpha 0.3). `pp0`/`tg0` (profile) seed the average on the first observation."""
    with join_or_open(conn):
        for name, obs, seed in (("pp", pp_obs, pp0), ("tg", tg_obs, tg0)):
            if obs is None or obs <= 0:
                continue
            key = f"ewma:{pool}:{name}"
            prev = state_get(conn, key)
            base = float(prev) if prev else (seed if seed is not None else obs)
            state_set(conn, key, repr(alpha * obs + (1 - alpha) * base), now)
