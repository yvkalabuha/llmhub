"""Destroy + zero confirmation, shared by controller and reaper (plan sections 3.5, 5).

Rules: adapters only have an idempotent `destroy`; a row is STOPPED only after the provider
shows no resource of ANY kind with its label (zero), seen twice at least `list_staleness_s`
apart when the adapter declares a stale-list window. Provider calls never run inside a
transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Literal

from llmhub.configuration.models import Config
from llmhub.lifecycle_manager.machine import set_provider_ref, transition
from llmhub.lifecycle_manager.safety import (
    block_provider,
    delete_key,
    get_key,
    halt,
    record_incident,
    set_key,
)
from llmhub.lifecycle_manager.states import State
from llmhub.persistence.db import assert_not_in_tx, tx
from llmhub.provider_adapters.base import (
    ProviderAdapter,
    RemoteResource,
    RemoteStatus,
)

Actor = Literal["controller", "reaper", "admin"]
StoppedHook = Callable[[sqlite3.Connection, sqlite3.Row, float], None]

# F-R2-1: after an unknown-outcome create, zero is confirmed not before
# first_create_called_at + UNKNOWN_GRACE_FACTOR * create_visibility_grace_s.
UNKNOWN_GRACE_FACTOR = 3


def on_stopped(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
    """Shared E20/E22 hook of controller and reaper (inside the final transaction, R1-4):
    finalize the ledger to `zero_confirmed_at`, close the instance reservations (hold/prewarm
    of a live lease are unbound, not closed), attribute the session cost to clients and drop
    the `launch_jobs:<id>` key."""
    from llmhub.budget_manager import reservations  # local: budget_manager -> lifecycle
    from llmhub.budget_manager.allocation import allocate_session
    from llmhub.cost_tracker import ledger

    delete_key(conn, f"launch_jobs:{row['id']}")
    ledger.finalize(conn, row["id"], float(row["zero_confirmed_at"] or now))
    reservations.release_instance(conn, row["id"], now, "instance_stopped")
    allocate_session(conn, row["id"])


def no_ref_wait_until(inst: sqlite3.Row, cfg: Config) -> float:
    """Earliest time zero may be confirmed for a row (0 when there is nothing to wait for).

    A row without `provider_ref` that ever called create waits `create_visibility_grace_s`
    after its LAST create call (the time is never cleared, R1-5); without a recorded time
    (older rows) the grace runs from `destroy_requested_at`. After an unknown outcome it also
    waits `first_create_called_at + UNKNOWN_GRACE_FACTOR * grace` (F-R2-1).
    """
    if inst["provider_ref"] is not None:
        return 0.0
    grace = cfg.timeouts.create_visibility_grace_s
    times = [
        float(t) for t in (inst["create_called_at"], inst["last_create_called_at"]) if t is not None
    ]
    wait = 0.0
    if times:
        wait = max(times) + grace
    elif int(inst["attempts"]) > 0:  # an earlier create without a recorded time
        base = inst["destroy_requested_at"]
        wait = float(base if base is not None else inst["state_since"]) + grace
    if inst["create_unknown"] and inst["first_create_called_at"] is not None:
        wait = max(wait, float(inst["first_create_called_at"]) + UNKNOWN_GRACE_FACTOR * grace)
    return wait


async def _ref_is_ours(
    conn: sqlite3.Connection, adapter: ProviderAdapter, inst: sqlite3.Row, now: float
) -> bool:
    """R1-11: `provider_ref` is not listed under the row's label; destroy it only when `get`
    shows a resource with exactly this label. A ghost (None) needs no destroy; another label
    -> `ownership_mismatch` incident and no destroy; a failed `get` -> retried later."""
    ref = inst["provider_ref"]
    try:
        res = await adapter.get(ref)
    except Exception:  # noqa: BLE001 - adapters may raise anything; retried next pass
        return False
    if res is None:
        return False
    if res.label == inst["label"]:
        return True
    record_incident(
        conn, now, "ownership_mismatch", "critical", ref=inst["id"],
        dedup_key=f"ownership_mismatch:{ref}",
        details={"provider_ref": ref, "row_label": inst["label"], "resource_label": res.label},
    )  # fmt: skip
    return False


def _staleness(adapter: ProviderAdapter) -> float:
    """Optional adapter attribute `list_staleness_s` (default 0: one empty list is enough)."""
    return max(float(getattr(adapter, "list_staleness_s", 0.0) or 0.0), 0.0)


async def _owned_by_label(adapter: ProviderAdapter, label: str) -> list[RemoteResource]:
    """Resources of every kind carrying exactly this label."""
    return [r for r in await adapter.list_owned(label) if r.label == label]


def _backoff_s(cfg: Config, attempts: int) -> float:
    return min(cfg.retry.destroy_backoff_s * (2**attempts), cfg.retry.destroy_backoff_max_s)


async def _destroy_refs(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    refs: list[str],
    now: float,
    cfg: Config,
) -> bool:
    """Call destroy for each ref (outside any tx). True when every call was accepted.

    A failure schedules the next try (`next_destroy_at`): exponential backoff capped at 120 s,
    or the provider's `retry_after_s`. Failures never open a breaker here.
    """
    assert_not_in_tx()
    ok = True
    retry_after: float | None = None
    err: str | None = None
    for ref in refs:
        try:
            await adapter.destroy(ref)
        except Exception as exc:  # noqa: BLE001 - adapters may raise anything
            ok = False
            ne = adapter.normalize_error(exc)
            err = ne.message[:200]
            if ne.retry_after_s is not None:
                retry_after = max(retry_after or 0.0, ne.retry_after_s)
    with tx(conn):
        row = conn.execute("SELECT destroy_attempts FROM instances WHERE id=?", (inst["id"],))
        attempts = int(row.fetchone()[0])
        nxt = now + (retry_after if retry_after is not None else _backoff_s(cfg, attempts))
        if ok:
            conn.execute(
                "UPDATE instances SET destroy_attempts=destroy_attempts+1, next_destroy_at=?"
                " WHERE id=?",
                (nxt, inst["id"]),
            )
        else:
            conn.execute(
                "UPDATE instances SET destroy_attempts=destroy_attempts+1, next_destroy_at=?,"
                " last_error=? WHERE id=?",
                (nxt, err, inst["id"]),
            )
    return ok


async def request_destroy(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    actor: Actor,
) -> None:
    """STOPPING: record the intent, call destroy; ACCEPTED/ALREADY_GONE -> PENDING_DELETE (E19).

    Destroys `provider_ref` plus any other resource that already carries the row's label
    (covers a create whose answer was lost). When destroy keeps failing, E19 still fires after
    `stop_confirm_s` so that a new cycle of the pool may start (the old row is then watched in
    PENDING_DELETE until zero or LEAKED).
    """
    assert_not_in_tx()
    first = inst["destroy_requested_at"] is None
    if first:
        with tx(conn):
            conn.execute(
                "UPDATE instances SET destroy_requested_at=? WHERE id=? AND destroy_requested_at IS NULL",
                (now, inst["id"]),
            )
    requested_at = now if first else float(inst["destroy_requested_at"])
    accepted = False
    if inst["next_destroy_at"] is None or now >= float(inst["next_destroy_at"]):
        refs: list[str] = []
        try:
            refs += [r.ref for r in await _owned_by_label(adapter, inst["label"])]
        except Exception:  # noqa: BLE001, S110 - listing failed: still destroy the known ref
            pass
        ref = inst["provider_ref"]
        if ref is not None and ref not in refs and await _ref_is_ours(conn, adapter, inst, now):
            refs.insert(0, ref)
        refs = list(dict.fromkeys(refs))
        accepted = await _destroy_refs(conn, adapter, inst, refs, now, cfg)
    if accepted or now - requested_at >= cfg.timeouts.stop_confirm_s:
        with tx(conn):
            transition(conn, inst["id"], State.STOPPING, State.PENDING_DELETE, "E19", now, actor,
                       destroy_accepted_at=now,
                       reason="E19" if accepted else "stop_confirm_timeout")  # fmt: skip


async def adopt_ref_or_destroy(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    instance_id: str,
    res: RemoteResource,
    now: float,
) -> bool:
    """A create answer arrived: store the ref WITHOUT a state CAS; if the row has left
    PROVISIONING (kill, timeout) destroy the ref at once. True when the ref is adopted.

    Exception (R1-12 root cause): the row already carries THIS ref and has moved on to a
    live state (another actor adopted the same resource and did E2): nothing to store and
    nothing to destroy. A row that already has another ref keeps it (no overwrite)."""
    assert_not_in_tx()
    with tx(conn):
        row = conn.execute(
            "SELECT state, provider_ref FROM instances WHERE id=?", (instance_id,)
        ).fetchone()
        state = State(row["state"])
        if state != State.PROVISIONING and row["provider_ref"] == res.ref:
            return False  # already ours and live (or in teardown, which destroys it)
        if state == State.PROVISIONING or row["provider_ref"] is None:
            state = set_provider_ref(conn, instance_id, res.ref, res.host_id, res.price_usd_h, now)
    if state == State.PROVISIONING:
        return True
    try:
        await adapter.destroy(res.ref)
    except Exception as exc:  # noqa: BLE001 - adapters may raise anything
        record_incident(
            conn, now, "late_ref_destroy_failed", "high", ref=instance_id,
            dedup_key=f"late_ref:{res.ref}",
            details={"state": state.value, "error": adapter.normalize_error(exc).message[:200]},
        )  # fmt: skip
    return False


async def confirm_zero(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    actor: Actor,
    *,
    on_stopped: StoppedHook | None = None,
) -> bool:
    """PENDING_DELETE/LEAKED -> STOPPED (E20/E22) once the provider shows zero resources.

    Zero = no resource of any kind with the label (instance, volume, other) and no remaining
    spend on `provider_ref`, observed twice at least `list_staleness_s` apart. Leftovers are
    destroyed by their own ref (backoff via `next_destroy_at`). A row without `provider_ref`
    also waits `no_ref_wait_until` (grace after the last create; after an unknown outcome
    also first create + 3 * grace); resources that show up meanwhile are destroyed at once.
    `on_stopped(conn, row, now)` runs inside the final transaction (ledger finalize, etc.).
    """
    assert_not_in_tx()
    src = State(inst["state"])
    if src not in (State.PENDING_DELETE, State.LEAKED):
        return False
    zero_key = f"zero_seen:{inst['id']}"
    try:
        owned = await _owned_by_label(adapter, inst["label"])
        spending = False
        if not owned and inst["provider_ref"] is not None:
            res = await adapter.get(inst["provider_ref"])
            spending = res is not None and (
                res.status != RemoteStatus.TERMINATED or (res.spend_rate_usd_h or 0.0) > 0
            )
            if spending and res is not None and res.label != inst["label"]:  # R1-11
                record_incident(
                    conn, now, "ownership_mismatch", "critical", ref=inst["id"],
                    dedup_key=f"ownership_mismatch:{inst['provider_ref']}",
                    details={"provider_ref": inst["provider_ref"], "row_label": inst["label"],
                             "resource_label": res.label},
                )  # fmt: skip
                return False  # not ours: never destroyed, never confirmed as zero
    except Exception:  # noqa: BLE001 - adapters may raise anything
        return False  # cannot see the provider: not zero
    if owned or spending:
        with tx(conn):
            delete_key(conn, zero_key)
        if inst["next_destroy_at"] is None or now >= float(inst["next_destroy_at"]):
            refs = [r.ref for r in owned]
            if spending and inst["provider_ref"] is not None:
                refs.append(inst["provider_ref"])
            await _destroy_refs(conn, adapter, inst, list(dict.fromkeys(refs)), now, cfg)
        return False
    if now < no_ref_wait_until(inst, cfg):  # a create may still become visible: not zero yet
        return False
    stale = _staleness(adapter)
    seen = get_key(conn, zero_key)
    if stale > 0:
        if seen is None:
            with tx(conn):
                set_key(conn, zero_key, repr(now), now)
            return False
        if now - float(seen) < stale:
            return False
    trigger = "E22" if src == State.LEAKED else "E20"
    with tx(conn):
        ok = transition(conn, inst["id"], src, State.STOPPED, trigger, now, actor,
                        zero_confirmed_at=now)  # fmt: skip
        if ok:
            delete_key(conn, zero_key)
            row = conn.execute("SELECT * FROM instances WHERE id=?", (inst["id"],)).fetchone()
            if on_stopped is not None:
                on_stopped(conn, row, now)
            from llmhub.budget_manager import reservations  # local: budget -> lifecycle

            reservations.release_instance(conn, inst["id"], now, "instance_stopped")
    return ok


def mark_leaks(conn: sqlite3.Connection, now: float, cfg: Config, actor: Actor) -> list[str]:
    """PENDING_DELETE without zero for more than `leak_after_s` -> LEAKED (E21).

    Writes a `leak` critical incident, blocks the provider and HALTs the controller.
    HALTED and the block are lifted only manually, even after E22.
    """
    leaked: list[str] = []
    with tx(conn):
        rows = conn.execute("SELECT * FROM instances WHERE state='PENDING_DELETE' ORDER BY id")
        for r in rows.fetchall():
            started = r["destroy_requested_at"]
            if started is None:
                started = (
                    r["destroy_accepted_at"]
                    if r["destroy_accepted_at"] is not None
                    else r["state_since"]
                )
            if now - float(started) <= cfg.timeouts.leak_after_s:
                continue
            if not transition(conn, r["id"], State.PENDING_DELETE, State.LEAKED, "E21", now,
                              actor, reason="leak"):  # fmt: skip
                continue
            record_incident(
                conn, now, "leak", "critical", ref=r["id"], dedup_key=f"leak:{r['id']}",
                details={"provider": r["provider"], "label": r["label"],
                         "provider_ref": r["provider_ref"]},
            )  # fmt: skip
            block_provider(conn, r["provider"], "leak", now)
            halt(conn, "leak", now)
            leaked.append(str(r["id"]))
    return leaked
