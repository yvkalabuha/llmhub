"""Reaper: independent watchdog run as its own process (plan section 3.10).

`Reaper.run_once()` order:
1. KILL file -> `kill_all(actor="reaper")`;
2. hard cap: accrue by CAS on `accrued_to` (no double count with the controller), then
   `spent(month)` (live billable instances are accrued to now) >= `hard_cap_month` -> kill;
3. teardown: stale STOPPING rows get destroy, PENDING_DELETE/LEAKED get `confirm_zero`;
4. `mark_leaks`;
5. orphan sweep (`orphan_sweep`, also run by the controller at start);
6. max_lifetime fuse: `max_lifetime_at + 600 s` -> STOPPING + destroy;
7. stale controller heartbeat (> 10 min) with live instances -> incident.
Foreign resources (no valid HMAC label) are never touched.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from llmhub.budget_manager import reservations
from llmhub.budget_manager.limits import windows
from llmhub.clock import Clock
from llmhub.configuration.models import Config
from llmhub.cost_tracker import ledger
from llmhub.cost_tracker.reconcile import reconcile
from llmhub.killswitch import KillReport, check_kill_file, kill_all
from llmhub.lifecycle_manager import machine, teardown
from llmhub.lifecycle_manager.recovery import destroy_duplicates
from llmhub.lifecycle_manager.safety import (
    delete_key,
    get_key,
    halt,
    record_incident,
    set_key,
)
from llmhub.lifecycle_manager.states import LIVE_IN_INDEX, State
from llmhub.persistence.db import assert_not_in_tx, tx
from llmhub.provider_adapters.base import ProviderAdapter, RemoteResource
from llmhub.provider_adapters.guarded import CircuitBreaker, GuardedAdapter
from llmhub.provider_adapters.labels import verify_label

Actor = Literal["controller", "reaper", "admin"]

UNKNOWN_DESTROY_AFTER_S = 1800.0  # Q1: auto-destroy 30 min after detection
UNKNOWN_RECONCILE_S = 900.0  # Q1: billing reconcile every 15 min while it lives
LIFETIME_FUSE_S = 600.0
HEARTBEAT_STALE_S = 600.0


@dataclass
class SweepReport:
    destroyed: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


@dataclass
class ReaperReport:
    kill: KillReport | None = None
    hard_cap: bool = False
    spent_month: float = 0.0
    stopped: list[str] = field(default_factory=list)
    leaked: list[str] = field(default_factory=list)
    fused: list[str] = field(default_factory=list)
    sweep: SweepReport = field(default_factory=SweepReport)
    stale_controller: bool = False


def _on_stopped(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
    ledger.finalize(conn, row["id"], float(row["zero_confirmed_at"] or now))
    reservations.release_instance(conn, row["id"], now, "instance_stopped")


async def _destroy(adapter: ProviderAdapter, ref: str) -> bool:
    try:
        await adapter.destroy(ref)
    except Exception:  # noqa: BLE001 - adapters may raise anything; retried next run
        return False
    return True


async def _unknown_resource(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    res: RemoteResource,
    now: float,
    cfg: Config,
    report: SweepReport,
) -> None:
    """Q1: valid HMAC, no row, normal mode -> critical incident + HALT; billing reconcile
    every 15 min while it lives; auto-destroy after 30 min (the label verified with the
    current secret, i.e. `local_secret_id` matches)."""
    key = f"unknown_seen:{res.ref}"
    first = get_key(conn, key)
    if first is None:
        with tx(conn):
            set_key(conn, key, repr(now), now)
            record_incident(
                conn, now, "unknown_owned_resource", "critical", ref=res.ref,
                dedup_key=f"unknown_owned:{res.ref}",
                details={"provider": adapter.name, "label": res.label},
            )  # fmt: skip
            halt(conn, "unknown_owned_resource", now)
        report.unknown.append(res.ref)
        first_seen = now
    else:
        first_seen = float(first)
        report.unknown.append(res.ref)
    last = get_key(conn, f"unknown_reconcile_at:{adapter.name}")
    if last is None or now - float(last) >= UNKNOWN_RECONCILE_S:
        with tx(conn):
            set_key(conn, f"unknown_reconcile_at:{adapter.name}", repr(now), now)
        try:
            await reconcile(conn, adapter, now, cfg)
        except Exception:  # noqa: BLE001, S110 - best effort; the incident is already open
            pass
    if now - first_seen >= UNKNOWN_DESTROY_AFTER_S and await _destroy(adapter, res.ref):
        report.destroyed.append(res.ref)
        with tx(conn):
            delete_key(conn, key)
            record_incident(
                conn, now, "unknown_owned_destroyed", "high", ref=res.ref,
                dedup_key=f"unknown_destroyed:{res.ref}", details={"provider": adapter.name},
            )  # fmt: skip


async def orphan_sweep(
    conn: sqlite3.Connection,
    adapters: Mapping[str, ProviderAdapter],
    secret: bytes,
    now: float,
    cfg: Config,
    actor: Actor,
) -> SweepReport:
    """Resources with a valid HMAC label: orphans and duplicates (sections 3.9, 3.10, Q1).

    - row STOPPED/ERROR, or no row in safe mode -> destroy;
    - live row with a listed `provider_ref` and this resource has another ref -> duplicate:
      destroy + `duplicate_resource` incident;
    - no row in normal mode -> Q1 (`_unknown_resource`).
    A failed listing destroys nothing.
    """
    assert_not_in_tx()
    report = SweepReport()
    safe = get_key(conn, "safe_mode") == "1"
    for name, adapter in adapters.items():
        try:
            owned = await adapter.list_owned("llmhub-")
        except Exception:  # noqa: BLE001, S112 - adapters may raise anything
            continue
        listed = {r.ref for r in owned}
        for res in owned:
            if not verify_label(res.label, secret):
                continue  # foreign: never touched
            row = conn.execute("SELECT * FROM instances WHERE label=?", (res.label,)).fetchone()
            if row is None:
                if safe:
                    if await _destroy(adapter, res.ref):
                        report.destroyed.append(res.ref)
                        record_incident(
                            conn, now, "orphan_destroyed", "warning", ref=res.ref,
                            dedup_key=f"orphan:{res.ref}", details={"safe_mode": True},
                        )  # fmt: skip
                elif actor == "reaper":
                    await _unknown_resource(conn, adapter, res, now, cfg, report)
                else:  # controller at start: report only, the reaper owns Q1 timers
                    record_incident(
                        conn, now, "unknown_owned_resource", "critical", ref=res.ref,
                        dedup_key=f"unknown_owned:{res.ref}",
                        details={"provider": name, "label": res.label},
                    )  # fmt: skip
                    halt(conn, "unknown_owned_resource", now)
                    report.unknown.append(res.ref)
                continue
            state = State(row["state"])
            if state in (State.STOPPED, State.ERROR):
                if await _destroy(adapter, res.ref):
                    report.destroyed.append(res.ref)
                continue
            if (
                state in LIVE_IN_INDEX
                and row["provider_ref"] is not None
                and row["provider_ref"] in listed
                and res.ref != row["provider_ref"]
            ):
                report.duplicates += await destroy_duplicates(conn, adapter, row, [res], now)
    return report


class Reaper:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        adapters: Mapping[str, ProviderAdapter],
        clock: Clock,
        secret: bytes,
        *,
        guard: bool = True,
        kill_deadline_s: float = 45.0,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.secret = secret
        self.kill_deadline_s = kill_deadline_s
        self.adapters: dict[str, ProviderAdapter] = {}
        for name, a in adapters.items():
            if guard and not isinstance(a, GuardedAdapter):
                breaker = CircuitBreaker(conn, name, clock, cfg.retry)
                a = GuardedAdapter(a, clock, cfg.retry, breaker, actor="reaper")
            self.adapters[name] = a

    async def _kill(self, reason: str) -> KillReport:
        return await kill_all(
            self.conn, self.adapters, self.secret, self.clock, self.cfg, "reaper",
            self.kill_deadline_s, reason=reason,
        )  # fmt: skip

    async def run_once(self) -> ReaperReport:
        assert_not_in_tx()
        report = ReaperReport()
        if check_kill_file(self.cfg.data_dir):
            report.kill = await self._kill("kill_file")
        now = self.clock.now()
        # 2. hard cap, independent of the controller
        ledger.accrue(self.conn, now)
        w = windows(now, self.cfg.budget.timezone)
        report.spent_month = ledger.spent(self.conn, w.month_start, w.month_end)
        if report.spent_month >= self.cfg.budget.hard_cap_month:
            report.hard_cap = True
            record_incident(
                self.conn, now, "hard_cap", "critical", dedup_key=f"hard_cap:{w.month_key}",
                details={"spent": round(report.spent_month, 4)},
            )  # fmt: skip
            report.kill = await self._kill("hard_cap")
            now = self.clock.now()
        # 3. teardown that the controller may not be doing
        for row in self.conn.execute(
            "SELECT * FROM instances WHERE state IN ('STOPPING','PENDING_DELETE','LEAKED')"
            " ORDER BY id"
        ).fetchall():
            adapter = self.adapters.get(row["provider"])
            if adapter is None:
                continue
            if row["state"] == State.STOPPING:
                if now - float(row["state_since"]) >= self.cfg.timeouts.stop_confirm_s:
                    await teardown.request_destroy(self.conn, adapter, row, now, self.cfg,
                                                   "reaper")  # fmt: skip
            elif await teardown.confirm_zero(self.conn, adapter, row, now, self.cfg, "reaper",
                                             on_stopped=_on_stopped):  # fmt: skip
                report.stopped.append(row["id"])
        # 4. leaks
        report.leaked = teardown.mark_leaks(self.conn, now, self.cfg, "reaper")
        # 5. orphans and duplicates
        report.sweep = await orphan_sweep(
            self.conn, self.adapters, self.secret, now, self.cfg, "reaper"
        )
        # 6. max_lifetime fuse
        report.fused = await self._lifetime_fuse(now)
        # 7. controller heartbeat
        report.stale_controller = self._check_heartbeat(now)
        return report

    async def _lifetime_fuse(self, now: float) -> list[str]:
        marks = ",".join("?" for _ in LIVE_IN_INDEX)
        rows = self.conn.execute(
            f"SELECT * FROM instances WHERE state IN ({marks}) AND state != 'STOPPING'"
            " AND max_lifetime_at + ? <= ? ORDER BY id",
            (*sorted(LIVE_IN_INDEX), LIFETIME_FUSE_S, now),
        ).fetchall()
        fused: list[str] = []
        for row in rows:
            src = State(row["state"])
            edge = "E18" if src == State.ERROR else "E15"
            with tx(self.conn):
                ok = machine.transition(self.conn, row["id"], src, State.STOPPING, edge, now,
                                        "reaper", stop_reason="lifetime_fuse",
                                        reason="lifetime_fuse")  # fmt: skip
                if ok:
                    machine.requeue_running_jobs(self.conn, now, instance_id=row["id"],
                                                 count_attempt=False, reason="lifetime_fuse")  # fmt: skip
            if not ok:
                continue
            fused.append(row["id"])
            adapter = self.adapters.get(row["provider"])
            fresh = self.conn.execute("SELECT * FROM instances WHERE id=?", (row["id"],)).fetchone()
            if adapter is not None:
                await teardown.request_destroy(self.conn, adapter, fresh, now, self.cfg, "reaper")
        return fused

    def _check_heartbeat(self, now: float) -> bool:
        live = self.conn.execute(
            "SELECT count(*) FROM instances WHERE state != 'STOPPED'"
        ).fetchone()[0]
        if not live:
            return False
        hb = get_key(self.conn, "heartbeat")
        if hb is not None and now - float(hb) <= HEARTBEAT_STALE_S:
            return False
        record_incident(
            self.conn, now, "controller_stale", "high",
            dedup_key=f"controller_stale:{int(now // HEARTBEAT_STALE_S)}",
            details={"last_heartbeat": None if hb is None else float(hb)},
        )  # fmt: skip
        return True

    async def run_loop(self, interval_s: float, stop: asyncio.Event | None = None) -> None:
        """`--loop`: one pass every `interval_s` (<= 15 s, so a KILL is honoured in 60 s)."""
        while stop is None or not stop.is_set():
            started = self.clock.now()
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the watchdog must keep running
                try:
                    record_incident(
                        self.conn, self.clock.now(), "reaper_error", "high",
                        dedup_key=f"reaper_error:{int(started // 3600)}",
                        details={"error": type(exc).__name__},
                    )  # fmt: skip
                except Exception:  # noqa: BLE001, S110 - DB busy: next pass
                    pass
            await self.clock.sleep_until(started + interval_s)
