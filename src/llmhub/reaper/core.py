"""Reaper: independent watchdog run as its own process (plan section 3.10).

`Reaper.run_once()` order:
1. KILL file -> `kill_all(actor="reaper")`;
2. hard cap: accrue by CAS on `accrued_to` (no double count with the controller), then
   `spent(month)` (live billable instances are accrued to now) >= `hard_cap_month` -> kill;
3. teardown: stale STOPPING rows get destroy, PENDING_DELETE/LEAKED get `confirm_zero`;
4. `mark_leaks`;
5. orphan sweep (`orphan_sweep`, also run by the controller at start);
6. max_lifetime fuse: `max_lifetime_at + 600 s` -> STOPPING + destroy;
7. stale controller heartbeat (> 10 min) with live instances -> incident; older than
   CONTROLLER_DEAD_STOP_S -> live instances without running jobs go to STOPPING (L8).
Every step is isolated (L1): an exception in one is recorded (`reaper_step_failed`) and the
next steps still run. A KILL file / hard cap that persists runs the 45 s `kill_all` once per
process, then one non-blocking `kill_pass` per run (L2).
Foreign resources (no valid HMAC label) are never touched.

T-371 (F4/F7): Docker does not restart an *unhealthy* container, so the process guards itself:
`Watchdog` (a daemon thread) calls `os._exit(70)` when no pass finished for
`stall_seconds(loop)`; every finished pass `beat()`s and rewrites the heartbeat file that the
container healthcheck (`python -m llmhub.reaper healthcheck`) reads.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from llmhub.budget_manager.limits import windows
from llmhub.clock import Clock
from llmhub.configuration.models import Config
from llmhub.cost_tracker import ledger
from llmhub.cost_tracker.reconcile import reconcile
from llmhub.killswitch import (
    KillReport,
    begin_kill,
    check_kill_file,
    emergency_kill_pass,
    kill_all,
    kill_pass,
)
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
from llmhub.persistence.db import assert_not_in_tx, connect, migrate, tx
from llmhub.provider_adapters.base import ProviderAdapter, RemoteResource
from llmhub.provider_adapters.guarded import CircuitBreaker, GuardedAdapter
from llmhub.provider_adapters.labels import verify_label

Actor = Literal["controller", "reaper", "admin"]

STALL_FACTOR = 3
WATCHDOG_EXIT_CODE = 70  # EX_SOFTWARE; Docker `--restart unless-stopped` starts a new one


def stall_seconds(loop_s: float, *, kill_deadline_s: float = 45.0) -> float:
    """No finished pass for this long = hung. At least STALL_FACTOR loops, and long enough
    for one blocking `kill_all` (kill_deadline_s) plus two passes, so a kill is never cut."""
    return max(STALL_FACTOR * loop_s, kill_deadline_s + 2 * loop_s)


def write_heartbeat(path: Path) -> None:
    """Atomic rewrite (tmp + os.replace); the mtime is the heartbeat."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{time.time():.3f}\n", encoding="ascii")
    os.replace(tmp, path)


def heartbeat_age(path: Path, now: float) -> float | None:
    try:
        return max(0.0, now - os.stat(path).st_mtime)
    except OSError:
        return None


class Watchdog:
    """Dead-man switch of the reaper process: `beat()` after every pass; a daemon thread
    exits the process with code 70 when the last beat is older than `stall_s` (a hung
    adapter call, a stuck DB lock). It fires once."""

    def __init__(
        self,
        stall_s: float,
        *,
        exit_fn: Callable[[int], object] = os._exit,
        monotonic: Callable[[], float] = time.monotonic,
        heartbeat: Path | None = None,
        check_every_s: float = 1.0,
    ) -> None:
        self.stall_s = stall_s
        self._exit = exit_fn
        self._mono = monotonic
        self.heartbeat = heartbeat
        self._every = check_every_s
        self._last = monotonic()
        self._fired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self) -> None:
        self._last = self._mono()
        if self.heartbeat is not None:
            try:
                write_heartbeat(self.heartbeat)
            except OSError as exc:  # the healthcheck will report it; the reaper keeps going
                print(f"reaper: heartbeat not written ({type(exc).__name__})", file=sys.stderr)

    def check(self) -> bool:
        if self._fired:
            return True
        idle = self._mono() - self._last
        if idle <= self.stall_s:
            return False
        self._fired = True
        print(f"reaper: no finished pass for {idle:.0f}s > {self.stall_s:.0f}s; exit "
              f"{WATCHDOG_EXIT_CODE}", file=sys.stderr, flush=True)  # fmt: skip
        self._exit(WATCHDOG_EXIT_CODE)
        return True

    def _run(self) -> None:
        while not self._stop.wait(self._every):
            if self.check():
                return

    def start(self) -> None:
        self._last = self._mono()
        self._thread = threading.Thread(target=self._run, name="reaper-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)

UNKNOWN_DESTROY_AFTER_S = 1800.0  # Q1: auto-destroy 30 min after detection
UNKNOWN_RECONCILE_S = 900.0  # Q1: billing reconcile every 15 min while it lives
LIFETIME_FUSE_S = 600.0
HEARTBEAT_STALE_S = 600.0
CONTROLLER_DEAD_STOP_S = 900.0  # L8: the reaper stops idle GPUs of a dead controller
UNKNOWN_TOMBSTONE_S = 86400.0  # L6: a destroyed unknown resource seen again is no new HALT


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
    dead_stopped: list[str] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)


# R1-4: one shared E20/E22 hook (finalize + release + allocate + launch_jobs key)
_on_stopped = teardown.on_stopped


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
    tomb = get_key(conn, f"unknown_destroyed:{res.ref}")
    if first is None and tomb is not None and now - float(tomb) < UNKNOWN_TOMBSTONE_S:
        # L6: already destroyed by Q1 and shown again by a stale list: destroy again, no HALT
        if await _destroy(adapter, res.ref):
            report.destroyed.append(res.ref)
        return
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
            set_key(conn, f"unknown_destroyed:{res.ref}", repr(now), now)
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
    *,
    unknown: bool = True,
) -> SweepReport:
    """Resources with a valid HMAC label: orphans and duplicates (sections 3.9, 3.10, Q1).

    - row STOPPED/ERROR, or no row in safe mode -> destroy;
    - live row with a listed `provider_ref` and this resource has another ref -> duplicate:
      destroy + `duplicate_resource` incident;
    - no row in normal mode -> Q1 (`_unknown_resource`); skipped with `unknown=False` (the
      controller's periodic sweep, F-R2-1).
    The reaper drops Q1 keys (`unknown_seen:<ref>`) of resources no longer listed (L6).
    A failed listing destroys nothing.
    """
    assert_not_in_tx()
    report = SweepReport()
    safe = get_key(conn, "safe_mode") == "1"
    all_listed: set[str] = set()
    all_ok = True
    for name, adapter in adapters.items():
        try:
            owned = await adapter.list_owned("llmhub-")
        except Exception:  # noqa: BLE001 - adapters may raise anything
            all_ok = False
            continue
        listed = {r.ref for r in owned}
        all_listed |= listed
        for res in owned:
            if not verify_label(res.label, secret):
                continue  # foreign: never touched
            row = conn.execute("SELECT * FROM instances WHERE label=?", (res.label,)).fetchone()
            if row is None:
                if not unknown and not safe:
                    continue
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
    if actor == "reaper" and all_ok:
        _forget_unlisted_unknown(conn, all_listed, now)
    return report


def _forget_unlisted_unknown(conn: sqlite3.Connection, listed: set[str], now: float) -> None:
    """L6: Q1 keys of resources that disappeared by themselves; old tombstones."""
    rows = conn.execute(
        "SELECT key, value FROM controller_state WHERE key LIKE 'unknown_seen:%'"
        " OR key LIKE 'unknown_destroyed:%'"
    ).fetchall()
    gone = [
        r["key"] for r in rows
        if (r["key"].startswith("unknown_seen:") and r["key"].split(":", 1)[1] not in listed)
        or (r["key"].startswith("unknown_destroyed:")
            and now - float(r["value"]) >= UNKNOWN_TOMBSTONE_S)
    ]  # fmt: skip
    if gone:
        with tx(conn):
            for k in gone:
                delete_key(conn, k)


def is_corrupt(exc: BaseException) -> bool:
    """A damaged/unreadable DB file (not a lock or a constraint): `sqlite3.DatabaseError`
    itself, e.g. "file is not a database", "database disk image is malformed"."""
    return type(exc) is sqlite3.DatabaseError


def open_reaper_db(path: Path, cfg: Config) -> sqlite3.Connection:
    """connect + quick_check + migrate; raises `sqlite3.DatabaseError` on a damaged file.
    The reaper never quarantines (that is the controller's `open_state_db`)."""
    conn = connect(path, busy_timeout_ms=cfg.db.reaper_busy_timeout_ms)
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok":
            raise sqlite3.DatabaseError(f"quick_check: {rows[0][0] if rows else 'no result'}")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _inode(path: Path) -> int | None:
    try:
        return os.stat(path).st_ino
    except FileNotFoundError:
        return None


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
        db_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.secret = secret
        self.kill_deadline_s = kill_deadline_s
        self.guard = guard
        self.raw_adapters = dict(adapters)
        self.db_path = db_path
        self._ino = None if db_path is None else _inode(db_path)
        self._killed_once = False
        self._need_reopen = False
        self._bind(conn)

    def _bind(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.adapters: dict[str, ProviderAdapter] = {}
        for name, a in self.raw_adapters.items():
            if self.guard and not isinstance(a, GuardedAdapter):
                breaker = CircuitBreaker(conn, name, self.clock, self.cfg.retry)
                a = GuardedAdapter(a, self.clock, self.cfg.retry, breaker, actor="reaper")
            self.adapters[name] = a

    def reopen(self) -> None:
        """New connection to `db_path` (raises `sqlite3.DatabaseError` on a damaged file)."""
        assert self.db_path is not None
        conn = open_reaper_db(self.db_path, self.cfg)
        old = self.conn
        self._bind(conn)
        self._ino = _inode(self.db_path)
        self._need_reopen = False
        try:
            old.close()
        except Exception:  # noqa: BLE001, S110 - the old handle may be broken already
            pass

    def _db_replaced(self) -> bool:
        """F-R2-4: the controller quarantined (renamed) state.db: our handle points at the
        old inode, so it must reconnect to see `safe_mode` and write to the live file."""
        return self.db_path is not None and _inode(self.db_path) != self._ino

    async def _kill(self, reason: str) -> KillReport:
        """L2: the blocking `kill_all` (<= kill_deadline_s) runs once per process; while the
        KILL file / hard cap persists, each later run makes one non-blocking pass."""
        if not self._killed_once:
            self._killed_once = True
            return await kill_all(
                self.conn, self.adapters, self.secret, self.clock, self.cfg, "reaper",
                self.kill_deadline_s, reason=reason,
            )  # fmt: skip
        now = self.clock.now()
        report = KillReport(stopped_rows=begin_kill(self.conn, now, "reaper", reason))
        report.remaining = await kill_pass(
            self.conn, self.adapters, self.secret, now, self.cfg, "reaper", report
        )
        report.completed = not report.remaining
        return report

    async def emergency_pass(self, exc: BaseException) -> list[str]:
        """F-R2-4: the DB is unusable. With a KILL file every HMAC-verified resource from
        `list_owned` is destroyed without the DB; always reported on stderr."""
        print(f"reaper: state.db unusable ({exc}); emergency mode", file=sys.stderr)
        self._need_reopen = True
        if not check_kill_file(self.cfg.data_dir):
            return []
        remaining = await emergency_kill_pass(self.raw_adapters, self.secret)
        print(f"reaper: emergency kill, remaining={len(remaining)}", file=sys.stderr)
        return remaining

    async def _isolated(
        self, report: ReaperReport, name: str, fn: Callable[[], Awaitable[None]]
    ) -> None:
        """L1: one step; its failure is recorded and the next steps still run. A damaged DB
        is re-raised (emergency mode in `run_loop`)."""
        try:
            await fn()
        except Exception as exc:
            if is_corrupt(exc):
                raise
            report.failed_steps.append(name)
            now = self.clock.now()
            try:
                record_incident(
                    self.conn, now, "reaper_step_failed", "high", ref=name,
                    dedup_key=f"reaper_step:{name}:{int(now // 3600)}",
                    details={"error": type(exc).__name__},
                )  # fmt: skip
            except Exception:  # noqa: BLE001, S110 - DB busy: the next pass records it
                pass

    async def run_once(self) -> ReaperReport:
        assert_not_in_tx()
        report = ReaperReport()
        kill_file = check_kill_file(self.cfg.data_dir)

        async def kill_step() -> None:
            report.kill = await self._kill("kill_file")

        async def hard_cap_step() -> None:
            now = self.clock.now()
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

        async def teardown_step() -> None:
            now = self.clock.now()
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
                elif await teardown.confirm_zero(self.conn, adapter, row, now, self.cfg,
                                                 "reaper", on_stopped=_on_stopped):  # fmt: skip
                    report.stopped.append(row["id"])

        async def leaks_step() -> None:
            report.leaked = teardown.mark_leaks(self.conn, self.clock.now(), self.cfg, "reaper")

        async def sweep_step() -> None:
            report.sweep = await orphan_sweep(
                self.conn, self.adapters, self.secret, self.clock.now(), self.cfg, "reaper"
            )

        async def fuse_step() -> None:
            report.fused = await self._lifetime_fuse(self.clock.now())

        async def heartbeat_step() -> None:
            now = self.clock.now()
            report.stale_controller = self._check_heartbeat(now)
            if report.stale_controller:
                report.dead_stopped = await self._stop_for_dead_controller(now)

        if kill_file:
            await self._isolated(report, "kill_file", kill_step)
        else:
            self._killed_once = False  # a later KILL / hard cap gets a full kill_all again
        await self._isolated(report, "hard_cap", hard_cap_step)
        await self._isolated(report, "teardown", teardown_step)
        await self._isolated(report, "leaks", leaks_step)
        await self._isolated(report, "orphan_sweep", sweep_step)
        await self._isolated(report, "lifetime_fuse", fuse_step)
        await self._isolated(report, "heartbeat", heartbeat_step)
        return report

    async def _stop_rows(self, rows: list[sqlite3.Row], now: float, reason: str) -> list[str]:
        """Live rows -> STOPPING (E15/E18, running jobs requeued, no attempt) + destroy."""
        stopped: list[str] = []
        for row in rows:
            src = State(row["state"])
            edge = "E18" if src == State.ERROR else "E15"
            with tx(self.conn):
                ok = machine.transition(self.conn, row["id"], src, State.STOPPING, edge, now,
                                        "reaper", stop_reason=reason, reason=reason)  # fmt: skip
                if ok:
                    machine.requeue_running_jobs(self.conn, now, instance_id=row["id"],
                                                 count_attempt=False, reason=reason)  # fmt: skip
            if not ok:
                continue
            stopped.append(row["id"])
            adapter = self.adapters.get(row["provider"])
            fresh = self.conn.execute("SELECT * FROM instances WHERE id=?", (row["id"],)).fetchone()
            if adapter is not None:
                await teardown.request_destroy(self.conn, adapter, fresh, now, self.cfg, "reaper")
        return stopped

    async def _lifetime_fuse(self, now: float) -> list[str]:
        marks = ",".join("?" for _ in LIVE_IN_INDEX)
        rows = self.conn.execute(
            f"SELECT * FROM instances WHERE state IN ({marks}) AND state != 'STOPPING'"
            " AND max_lifetime_at + ? <= ? ORDER BY id",
            (*sorted(LIVE_IN_INDEX), LIFETIME_FUSE_S, now),
        ).fetchall()
        return await self._stop_rows(rows, now, "lifetime_fuse")

    async def _stop_for_dead_controller(self, now: float) -> list[str]:
        """L8: controller heartbeat older than CONTROLLER_DEAD_STOP_S -> live instances without
        running jobs go to STOPPING (nobody serves them; they would bill until the fuse)."""
        hb = get_key(self.conn, "heartbeat")
        if hb is None or now - float(hb) < CONTROLLER_DEAD_STOP_S:
            return []
        marks = ",".join("?" for _ in LIVE_IN_INDEX)
        rows = self.conn.execute(
            f"SELECT * FROM instances i WHERE state IN ({marks}) AND state NOT IN"
            " ('STOPPING','ERROR') AND NOT EXISTS (SELECT 1 FROM jobs j"
            " WHERE j.instance_id = i.id AND j.status='running') ORDER BY id",
            sorted(LIVE_IN_INDEX),
        ).fetchall()
        stopped = await self._stop_rows(rows, now, "controller_dead")
        if stopped:
            record_incident(
                self.conn, now, "controller_dead_stop", "high",
                dedup_key=f"controller_dead_stop:{int(now // HEARTBEAT_STALE_S)}",
                details={"instances": stopped, "last_heartbeat": float(hb)},
            )  # fmt: skip
        return stopped

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

    async def run_loop(
        self,
        interval_s: float,
        stop: asyncio.Event | None = None,
        *,
        on_pass: Callable[[], None] | None = None,
    ) -> None:
        """`--loop`: one pass every `interval_s` (<= 15 s, so a KILL is honoured in 60 s).

        A damaged DB (F-R2-4) switches the pass to `emergency_pass`; with `db_path` the
        connection is reopened when state.db was replaced (quarantine) or after an error.
        `on_pass` runs after every finished pass, failed or not (watchdog beat, T-371)."""
        while stop is None or not stop.is_set():
            started = self.clock.now()
            try:
                if self.db_path is not None and (self._need_reopen or self._db_replaced()):
                    self.reopen()
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the watchdog must keep running
                if is_corrupt(exc):
                    await self.emergency_pass(exc)
                else:
                    try:
                        record_incident(
                            self.conn, self.clock.now(), "reaper_error", "high",
                            dedup_key=f"reaper_error:{int(started // 3600)}",
                            details={"error": type(exc).__name__},
                        )  # fmt: skip
                    except Exception as inner:  # noqa: BLE001 - DB busy or damaged
                        if is_corrupt(inner):
                            await self.emergency_pass(inner)
            if on_pass is not None:
                on_pass()
            await self.clock.sleep_until(started + interval_s)
