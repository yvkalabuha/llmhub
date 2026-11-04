"""Router inputs derived from state: cloud view (warm pool, t_cold_p90, provider blocks, breaker,
HALTED/KILL, budget stage, weights hold) and `Retry-After` (T-369 plan 3.5, stream R).

`CloudState` reads the DB only; the global part (halted, KILL file, budget stage, provider
accounts) is cached for 5 s, the per-pool part (warm instance, speeds, hold) is read live.
Local ETAs come from `LocalWorker.eta_s` (stream H) through the `EtaSource` protocol.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from llmhub.budget_manager.estimate import get_speeds, t_cold_p90
from llmhub.budget_manager.limits import Stage, stage
from llmhub.clock import Clock
from llmhub.cloud_manager.provisioner import weights_hold_until
from llmhub.configuration.models import Config, PoolConfig
from llmhub.killswitch import check_kill_file
from llmhub.lifecycle_manager.safety import is_halted
from llmhub.model_router.rules import CONTROLLER_HALTED, CloudView
from llmhub.provider_adapters.base import UnavailableReason
from llmhub.task_queue.models import Priority

CACHE_TTL_S = 5.0
WARM_STATES = ("READY", "IDLE", "BUSY")


class EtaSource(Protocol):
    """The slice of `LocalWorker` the router needs (stream H)."""

    def eta_s(
        self, alias: str, tokens_in: int, max_tokens: int, prio_rank: int, now: float
    ) -> float | None: ...


@dataclass(frozen=True)
class _Global:
    at: float
    halted: bool
    stage: Stage
    accounts: dict[
        str, tuple[str | None, str, float | None]
    ]  # provider -> blocked, circuit, opened


def _reason(raw: str) -> UnavailableReason:
    try:
        return UnavailableReason(raw)
    except ValueError:
        return UnavailableReason.PROVIDER_API_ERROR


class CloudState:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        clock: Clock,
        has_adapter: Callable[[str], bool] | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self._has_adapter = has_adapter
        self._cache: _Global | None = None

    # ---- cached global part --------------------------------------------------------------
    def _global(self, now: float) -> _Global:
        c = self._cache
        if c is not None and 0 <= now - c.at < CACHE_TTL_S:
            return c
        accounts = {
            str(r["provider"]): (
                r["blocked_reason"],
                str(r["circuit_state"]),
                r["circuit_opened_at"],
            )
            for r in self.conn.execute(
                "SELECT provider, blocked_reason, circuit_state, circuit_opened_at"
                " FROM provider_accounts"
            )
        }
        halted = is_halted(self.conn) or check_kill_file(self.cfg.data_dir)
        self._cache = _Global(now, halted, stage(self.conn, now, self.cfg), accounts)
        return self._cache

    def invalidate(self) -> None:
        self._cache = None

    def budget_stage(self) -> Stage:
        return self._global(self.clock.now()).stage

    def halted(self) -> bool:
        return self._global(self.clock.now()).halted

    # ---- per pool ------------------------------------------------------------------------
    def pool_for(self, alias: str) -> PoolConfig | None:
        for p in self.cfg.pools.values():
            if alias in p.models:
                return p
        return None

    def _warm_instance(self, pool: str) -> sqlite3.Row | None:
        marks = ",".join("?" for _ in WARM_STATES)
        row: sqlite3.Row | None = self.conn.execute(
            f"SELECT price_usd_h FROM instances WHERE pool=? AND state IN ({marks}) LIMIT 1",
            (pool, *WARM_STATES),
        ).fetchone()
        return row

    def view(self, alias: str, tokens_in: int, max_tokens: int, priority: Priority) -> CloudView:
        now = self.clock.now()
        pool = self.pool_for(alias)
        if pool is None:
            return CloudView(None, 0, False, 0.0, None, 0.0)
        am = self.cfg.models.get(alias)
        ctx = am.cloud_ctx_tokens if am is not None else 65536
        pp, tg = get_speeds(self.conn, pool.name, pool.pp_tps, pool.tg_tps)
        work = tokens_in / pp + max_tokens / tg
        warm_row = self._warm_instance(pool.name)
        warm = warm_row is not None
        t_cold = 0.0 if warm else t_cold_p90(self.conn, pool.name, pool.cold_start_s_default)
        price = (
            float(warm_row["price_usd_h"] or pool.max_price_usd_h)
            if warm_row
            else pool.max_price_usd_h
        )
        cost = price * (work + t_cold) / 3600.0
        unavailable, retry = self._unavailable(pool, priority, warm, now)
        return CloudView(pool.name, ctx, warm, t_cold + work, unavailable, cost, retry)

    def _unavailable(
        self, pool: PoolConfig, priority: Priority, warm: bool, now: float
    ) -> tuple[UnavailableReason | Literal["controller_halted"] | None, float | None]:
        g = self._global(now)
        if g.halted:
            return CONTROLLER_HALTED, None
        blocked, circuit, opened = g.accounts.get(pool.provider, (None, "closed", None))
        if blocked:
            return _reason(blocked), None
        if circuit == "open" and opened is not None:
            left = float(opened) + self.cfg.retry.breaker_open_s - now
            if left > 0:
                return UnavailableReason.PROVIDER_API_ERROR, left
        if self._has_adapter is not None and not self._has_adapter(pool.provider):
            return UnavailableReason.PROVIDER_API_ERROR, None
        # budget: NO_LAUNCH (F3): the cloud is closed to everyone, warm or cold (the controller
        # stops warm instances at 100 %); at URGENT_ONLY (P5, ADR-0138) the cloud,
        # warm or cold, is for urgent jobs only
        if g.stage >= Stage.NO_LAUNCH:
            return UnavailableReason.BUDGET_LIMIT_REACHED, None
        if (
            g.stage >= Stage.URGENT_ONLY
            and priority != Priority.URGENT
            and g.stage < Stage.NO_LAUNCH
        ):
            return UnavailableReason.BUDGET_LIMIT_REACHED, None
        if not warm:
            until = weights_hold_until(self.conn, pool.name, now, self.cfg)
            if until is not None:
                return UnavailableReason.DEPLOYMENT_ERROR, until - now
        return None, None


def retry_after_s(
    conn: sqlite3.Connection, cfg: Config, job: sqlite3.Row, worker: EtaSource, now: float
) -> int:
    """ETA of the job (local: worker queue; cloud: warm work, or t_cold_p90 + work), clamped."""
    lo, hi = cfg.api.retry_after_min_s, cfg.api.retry_after_max_s
    tin, tout = int(job["est_tokens_in"] or 0), int(job["est_tokens_out"] or 0)
    eta: float | None
    pool_name = job["pool"]
    if pool_name is None:
        eta = worker.eta_s(job["model_alias"], tin, tout, int(job["prio_rank"]), now)
    else:
        pc = cfg.pools.get(pool_name)
        if pc is None:
            eta = None
        else:
            pp, tg = get_speeds(conn, pool_name, pc.pp_tps, pc.tg_tps)
            warm = conn.execute(
                "SELECT 1 FROM instances WHERE pool=? AND state IN ('READY','IDLE','BUSY') LIMIT 1",
                (pool_name,),
            ).fetchone()
            cold = 0.0 if warm else t_cold_p90(conn, pool_name, pc.cold_start_s_default)
            eta = cold + tin / pp + tout / tg
    if eta is None or math.isnan(eta):
        return hi
    return int(min(hi, max(lo, math.ceil(eta))))
