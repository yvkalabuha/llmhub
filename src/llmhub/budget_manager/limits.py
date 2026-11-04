"""Budget gates: windows, committed spend, degradation stages, launch/job/hold checks, warnings."""

from __future__ import annotations

import json
import sqlite3
from calendar import monthrange
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum
from zoneinfo import ZoneInfo

from llmhub.budget_manager import accounts, reservations
from llmhub.budget_manager.estimate import JobLoad, LaunchEstimate, get_speeds, work_seconds
from llmhub.configuration.models import Config
from llmhub.cost_tracker.ledger import session_cost, spent
from llmhub.cost_tracker.txutil import add_incident, join_or_open
from llmhub.lifecycle_manager.safety import is_halted
from llmhub.provider_adapters.base import UnavailableReason
from llmhub.task_queue.models import Priority

_EPS = 1e-9


@dataclass(frozen=True)
class Gate:
    ok: bool
    reason: UnavailableReason | None = None
    detail: str = ""


class Stage(IntEnum):
    NORMAL = 0
    ECONOMY = 1  # >= 80 %: only noted here, the policy is applied by the router (T-369)
    URGENT_ONLY = 2  # >= 95 %
    NO_LAUNCH = 3  # >= 100 %


class HoldExceedsSessionBudget(Exception):
    """A hold whose cost to TTL exceeds budget_session is rejected at creation."""

    reason = UnavailableReason.BUDGET_LIMIT_REACHED

    def __init__(self, cost_usd: float, limit_usd: float) -> None:
        super().__init__(f"hold cost {cost_usd:.4f} > budget_session {limit_usd:.4f}")
        self.cost_usd = cost_usd
        self.limit_usd = limit_usd


@dataclass(frozen=True)
class Windows:
    day_start: float
    day_end: float
    month_start: float
    month_end: float
    day_of_month: int
    days_in_month: int
    day_key: str
    month_key: str


def windows(now: float, tz: str = "UTC") -> Windows:
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(now, zone)
    day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1)
    nxt_month = (month0 + timedelta(days=32)).replace(day=1)
    return Windows(
        day_start=day0.timestamp(),
        day_end=_next_local_midnight(day0, zone),
        month_start=month0.timestamp(),
        month_end=nxt_month.timestamp(),
        day_of_month=local.day,
        days_in_month=monthrange(local.year, local.month)[1],
        day_key=local.strftime("%Y-%m-%d"),
        month_key=local.strftime("%Y-%m"),
    )


def _next_local_midnight(day0: datetime, zone: ZoneInfo) -> float:
    nxt = (day0 + timedelta(days=1, hours=12)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.replace(tzinfo=zone).timestamp()


def _pools_of(cfg: Config, subbudget: str | None) -> tuple[str, ...] | None:
    if subbudget is None:
        return None
    return tuple(n for n, p in cfg.pools.items() if p.subbudget == subbudget)


def committed(
    conn: sqlite3.Connection,
    start: float,
    end: float,
    now: float,
    *,
    pools: tuple[str, ...] | None = None,
) -> float:
    """spent(window) + open reservation remainders (consumed parts are already in the ledger)."""
    _ = now
    return spent(conn, start, end, pools=pools) + reservations.open_remainder_total(
        conn, pools=pools
    )


def _ratios(conn: sqlite3.Connection, now: float, cfg: Config) -> tuple[float, float]:
    w = windows(now, cfg.budget.timezone)
    month = committed(conn, w.month_start, w.month_end, now) / cfg.budget.budget_month
    day = committed(conn, w.day_start, w.day_end, now) / cfg.budget.budget_day
    return month, day


def stage(conn: sqlite3.Connection, now: float, cfg: Config) -> Stage:
    ratio = max(_ratios(conn, now, cfg))
    b = cfg.budget
    if ratio + _EPS >= 1.0:
        return Stage.NO_LAUNCH
    if ratio + _EPS >= b.stage_urgent_only:
        return Stage.URGENT_ONLY
    if ratio + _EPS >= b.stage_economy:
        return Stage.ECONOMY
    return Stage.NORMAL


def _fail(reason: UnavailableReason, detail: str) -> Gate:
    return Gate(ok=False, reason=reason, detail=detail)


def _limits_gate(conn: sqlite3.Connection, amount: float, now: float, cfg: Config) -> Gate | None:
    """Day and month limits for an additional `amount` (shared by launch, job and hold)."""
    w = windows(now, cfg.budget.timezone)
    b = cfg.budget
    if committed(conn, w.day_start, w.day_end, now) + amount > b.budget_day + _EPS:
        return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, "day")
    if committed(conn, w.month_start, w.month_end, now) + amount > b.budget_month + _EPS:
        return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, "month")
    return None


def check_launch(
    conn: sqlite3.Connection,
    est: LaunchEstimate,
    provider: str,
    now: float,
    cfg: Config,
    priority: Priority,
    subbudget: str | None,
) -> Gate:
    """All checks of plan section 6, the first failed one gives the reason."""
    b = cfg.budget
    if is_halted(conn):
        return _fail(UnavailableReason.PROVIDER_API_ERROR, "controller_halted")
    blocked = accounts.is_blocked(conn, provider)
    if blocked is not None:
        return _fail(UnavailableReason(blocked), f"provider_blocked:{blocked}")
    st = stage(conn, now, cfg)
    if st == Stage.NO_LAUNCH:
        return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, "stage:no_launch")
    if st == Stage.URGENT_ONLY and priority != Priority.URGENT:
        return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, "stage:urgent_only")
    limit_fail = _limits_gate(conn, est.usd, now, cfg)
    if limit_fail is not None:
        return limit_fail
    if est.usd > b.budget_session + _EPS:
        return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, "session")
    if est.per_job and max(est.per_job.values()) > b.budget_job + _EPS:
        return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, "job")
    if subbudget is not None:
        cap = b.subbudgets.get(subbudget)
        if cap is not None:
            w = windows(now, b.timezone)
            sub = committed(conn, w.month_start, w.month_end, now, pools=_pools_of(cfg, subbudget))
            if sub + est.usd > cap + _EPS:
                return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, f"subbudget:{subbudget}")
    balance = accounts.last_balance(conn, provider)
    if balance is not None and (
        est.usd + b.reserve_usd > balance or balance < est.price_usd_h * b.min_balance_hours
    ):
        accounts.block_provider(conn, provider, str(UnavailableReason.INSUFFICIENT_BALANCE), now)
        return _fail(UnavailableReason.INSUFFICIENT_BALANCE, "balance")
    return Gate(ok=True)


def launch_gate(
    est: LaunchEstimate,
    provider: str,
    now: float,
    cfg: Config,
    priority: Priority,
    subbudget: str | None,
    op_id: str,
) -> Callable[[sqlite3.Connection], Gate]:
    """Gate for `insert_provisioning` (runs inside its transaction, before the INSERT).

    On success it also writes the `session` reservation for `inst-<op_id>`; the instance row
    follows in the same transaction (foreign keys are deferred to COMMIT), and a UNIQUE
    failure of the INSERT rolls the reservation back.
    """

    def gate(conn: sqlite3.Connection) -> Gate:
        g = check_launch(conn, est, provider, now, cfg, priority, subbudget)
        if g.ok:
            conn.execute("PRAGMA defer_foreign_keys=ON")
            reservations.reserve(conn, "session", op_id, est.usd, now, f"inst-{op_id}")
        return g

    return gate


# --- jobs on a running GPU -------------------------------------------------------------


def instance_disk_usd_h(inst: sqlite3.Row) -> float:
    if not inst["offer_json"]:
        return 0.0
    return float(json.loads(inst["offer_json"]).get("disk_usd_h", 0.0))


def _instance_rate(inst: sqlite3.Row) -> float:
    return (float(inst["price_usd_h"] or 0.0) + instance_disk_usd_h(inst)) / 3600.0


def idle_reserved(conn: sqlite3.Connection, inst: sqlite3.Row, cfg: Config) -> float:
    """USD already reserved for idle time on this instance, capped at one T_idle."""
    r = _instance_rate(inst)
    t_idle = cfg.pools[inst["pool"]].idle_timeout_s
    rows = conn.execute(
        "SELECT scope, ref_id, amount_usd, consumed_usd FROM reservations"
        " WHERE instance_id=? AND released_at IS NULL",
        (inst["id"],),
    ).fetchall()
    total = sum(
        reservations.remainder(x)
        for x in rows
        if x["scope"] == "session" or str(x["ref_id"]).endswith(":idle")
    )
    return min(r * t_idle, total)


def job_reservation(
    conn: sqlite3.Connection, inst: sqlite3.Row, job: JobLoad, cfg: Config
) -> tuple[float, float]:
    """(work_usd, idle_top_up_usd). A new job only tops idle up to one T_idle (not n*T_idle)."""
    pool = cfg.pools[inst["pool"]]
    pp, tg = get_speeds(conn, pool.name, pool.pp_tps, pool.tg_tps)
    r = _instance_rate(inst)
    work = r * work_seconds(job.tokens_in, job.max_tokens, pp, tg)
    top_up = max(0.0, r * pool.idle_timeout_s - idle_reserved(conn, inst, cfg))
    return work, top_up


def check_job_on_running(
    conn: sqlite3.Connection, instance_id: str, job: JobLoad, now: float, cfg: Config
) -> Gate:
    """Job on a live GPU: budget_job, budget_session (spent + open reservations), day, month."""
    inst = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
    if inst is None:
        return _fail(UnavailableReason.DEPLOYMENT_ERROR, "no_instance")
    work, top_up = job_reservation(conn, inst, job, cfg)
    amount = work + top_up
    b = cfg.budget
    if amount > b.budget_job + _EPS:
        return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, "job")
    in_flight = session_cost(conn, instance_id) + reservations.open_remainder_total(
        conn, instance_id=instance_id
    )
    if in_flight + amount > b.budget_session + _EPS:
        return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, "session")
    return _limits_gate(conn, amount, now, cfg) or Gate(ok=True)


def reserve_job_on_running(
    conn: sqlite3.Connection, instance_id: str, job: JobLoad, now: float, cfg: Config
) -> Gate:
    """check_job_on_running + job reservation (work row and optional `:idle` top-up row)."""
    with join_or_open(conn):
        g = check_job_on_running(conn, instance_id, job, now, cfg)
        if g.ok:
            inst = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
            work, top_up = job_reservation(conn, inst, job, cfg)
            reservations.reserve(conn, "job", job.job_id, work, now, instance_id)
            if top_up > 0:
                reservations.reserve(conn, "job", f"{job.job_id}:idle", top_up, now, instance_id)
    return g


# --- hold ------------------------------------------------------------------------------


def check_hold(
    conn: sqlite3.Connection,
    price_usd_h: float,
    ttl_s: float,
    now: float,
    cfg: Config,
    *,
    disk_usd_h: float = 0.0,
) -> Gate:
    """Hold creation. Cost to TTL > budget_session raises HoldExceedsSessionBudget unless
    `leases.hold_may_exceed_session`; then day/month limits are checked."""
    cost = (price_usd_h + disk_usd_h) / 3600.0 * ttl_s
    if cost > cfg.budget.budget_session + _EPS and not cfg.leases.hold_may_exceed_session:
        raise HoldExceedsSessionBudget(cost, cfg.budget.budget_session)
    if stage(conn, now, cfg) == Stage.NO_LAUNCH:
        return _fail(UnavailableReason.BUDGET_LIMIT_REACHED, "stage:no_launch")
    return _limits_gate(conn, cost, now, cfg) or Gate(ok=True)


def close_hold_prewarm_if_exhausted(conn: sqlite3.Connection, now: float, cfg: Config) -> int:
    """At 100 % hold and prewarm reservations are closed (their leases: lifecycle_manager)."""
    if stage(conn, now, cfg) != Stage.NO_LAUNCH:
        return 0
    return reservations.release_scopes(conn, ("hold", "prewarm"), now, "budget_100")


def must_stop_after_current(conn: sqlite3.Connection, now: float, cfg: Config) -> bool:
    """100 % of the day/month budget: the live session gets stop_after_current."""
    return stage(conn, now, cfg) == Stage.NO_LAUNCH


# --- warnings --------------------------------------------------------------------------


def warnings(conn: sqlite3.Connection, now: float, cfg: Config) -> list[str]:
    """Write deduplicated warning incidents (50/80/95 % of month and day, pace); return new keys."""
    b = cfg.budget
    w = windows(now, b.timezone)
    month, day = _ratios(conn, now, cfg)
    new: list[str] = []
    with join_or_open(conn):
        for window, ratio, period in (("month", month, w.month_key), ("day", day, w.day_key)):
            for level in b.warn_levels:
                if ratio + _EPS >= level:
                    key = f"budget:{window}:{round(level * 100)}:{period}"
                    if add_incident(
                        conn, now, "budget_warning", "warning", ref=window, dedup_key=key,
                        details_json=json.dumps({"ratio": round(ratio, 4)}),
                    ):  # fmt: skip
                        new.append(key)
        month_spent = spent(conn, w.month_start, w.month_end)
        expected = b.pace_factor * (w.day_of_month / w.days_in_month) * b.budget_month
        if month_spent > expected:
            key = f"budget_pace:{w.month_key}"
            if add_incident(
                conn, now, "budget_pace", "warning", ref="month", dedup_key=key,
                details_json=json.dumps({"spent": round(month_spent, 4)}),
            ):  # fmt: skip
                new.append(key)
    return new
