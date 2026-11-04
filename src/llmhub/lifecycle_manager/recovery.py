"""Restart reconciliation (plan section 8) and the live-duplicate rule (critic high, section 14).

For every non-STOPPED row the provider list is matched by label:
- resources with the row's label whose ref differs from `provider_ref` are duplicates of a
  live row (late result of an unknown-outcome create): destroy + incident `duplicate_resource`;
  with `provider_ref` NULL the oldest resource is adopted and the rest are duplicates;
- a `provider_ref` the provider does not know (ghost) while another resource carries the label:
  the row moves to the label's resource, the ghost is not touched (it does not exist).
Provider calls run outside transactions; a provider whose listing fails is skipped (nothing is
destroyed on a failed list).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from llmhub.cloud_manager.inference import InferenceBackend
from llmhub.configuration.models import Config
from llmhub.lifecycle_manager import leases, machine
from llmhub.lifecycle_manager.safety import record_incident, set_key
from llmhub.lifecycle_manager.states import State
from llmhub.persistence.db import assert_not_in_tx, tx
from llmhub.provider_adapters.base import ProviderAdapter, RemoteResource, RemoteStatus


@dataclass
class ReconcileReport:
    adopted: list[str] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)
    errored: list[str] = field(default_factory=list)
    requeued_jobs: list[str] = field(default_factory=list)


async def destroy_duplicates(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    extras: list[RemoteResource],
    now: float,
) -> list[str]:
    """Destroy resources that duplicate a live row; one `duplicate_resource` incident each."""
    assert_not_in_tx()
    done: list[str] = []
    for res in extras:
        try:
            await adapter.destroy(res.ref)
        except Exception:  # noqa: BLE001, S110 - retried on the next reconcile
            pass
        else:
            done.append(res.ref)
        record_incident(
            conn, now, "duplicate_resource", "high", ref=inst["id"], dedup_key=f"dup:{res.ref}",
            details={"label": inst["label"], "dup_ref": res.ref, "row_ref": inst["provider_ref"]},
        )  # fmt: skip
    return done


def split_duplicates(
    inst: sqlite3.Row, mine: list[RemoteResource], known: RemoteResource | None
) -> tuple[RemoteResource | None, list[RemoteResource]]:
    """(target, duplicates) among the resources carrying the row's label.

    `known` is what `get(provider_ref)` returned (None for no ref or a ghost ref).
    """
    ordered = sorted(mine, key=lambda r: (r.created_at, r.ref))
    if known is not None:
        return known, [r for r in ordered if r.ref != known.ref]
    if ordered:  # no ref yet, or a ghost ref: adopt the oldest resource with this label
        return ordered[0], ordered[1:]
    return None, []


async def _reconcile_row(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    backend: InferenceBackend,
    row: sqlite3.Row,
    owned: list[RemoteResource],
    now: float,
    cfg: Config,
    boot_id: str,
    report: ReconcileReport,
) -> None:
    state = State(row["state"])
    if state in (State.STOPPING, State.PENDING_DELETE, State.LEAKED):
        return  # teardown continues in the normal steps
    if state == State.ERROR:
        return  # E18 at the end
    iid = row["id"]
    mine = [r for r in owned if r.label == row["label"]]
    known: RemoteResource | None = None
    if row["provider_ref"] is not None:
        known = next((r for r in mine if r.ref == row["provider_ref"]), None)
        if known is None:
            try:
                known = await adapter.get(row["provider_ref"])
            except Exception:  # noqa: BLE001 - adapters may raise anything
                return  # cannot decide: leave the row for the next step
    target, extras = split_duplicates(row, mine, known)
    if extras:
        report.destroyed += await destroy_duplicates(conn, adapter, row, extras, now)
    alive = target is not None and target.status not in (
        RemoteStatus.TERMINATED,
        RemoteStatus.EXITED,
    )
    if target is not None and row["provider_ref"] != target.ref:
        machine.set_provider_ref(conn, iid, target.ref, target.host_id, target.price_usd_h, now)
    if target is not None and row["endpoint"] is None and target.endpoint is not None:
        with tx(conn):
            conn.execute("UPDATE instances SET endpoint=? WHERE id=?", (target.endpoint, iid))
    endpoint = row["endpoint"] or (target.endpoint if target else None)

    if state == State.PROVISIONING:
        if target is not None and alive:
            report.adopted.append(iid)
            if target.status == RemoteStatus.RUNNING:
                with tx(conn):
                    machine.transition(conn, iid, state, State.BOOTING, "E2", now, "controller",
                                       endpoint=endpoint)  # fmt: skip
        elif target is not None:  # terminated / exited resource
            if machine.mark_lost(conn, iid, now, "resource_gone", cfg=cfg):
                report.errored.append(iid)
        elif row["create_called_at"] is not None:  # unknown-outcome path
            if now < float(row["create_called_at"]) + cfg.timeouts.create_visibility_grace_s:
                return
            if row["attempts"] >= cfg.retry.create_max_attempts:
                if machine.mark_lost(conn, iid, now, "create_unknown_max_attempts", cfg=cfg):
                    report.errored.append(iid)
            else:  # retry create with the same label
                with tx(conn):
                    conn.execute(
                        "UPDATE instances SET create_called_at=NULL, create_unknown=1"
                        " WHERE id=?",
                        (iid,),
                    )
        return

    if state in (State.BOOTING, State.LOADING_MODEL):
        if alive:
            report.adopted.append(iid)
        elif machine.mark_lost(conn, iid, now, "resource_gone", cfg=cfg):
            report.errored.append(iid)
        return

    # READY / BUSY / IDLE
    healthy = False
    if alive and endpoint is not None:
        try:
            healthy = await backend.health(endpoint)
        except Exception:  # noqa: BLE001 - adapters may raise anything
            healthy = False
    if not healthy:
        if state == State.BUSY:
            # L4: a loss found during the restart reconcile is not the jobs' attempt: the
            # old-process jobs go back first without counting it, then E17
            report.requeued_jobs += machine.requeue_running_jobs(
                conn, now, instance_id=iid, not_worker=boot_id, count_attempt=False,
                reason="restart",
            )  # fmt: skip
        cause = "health_false" if alive else "resource_gone"
        if machine.mark_lost(conn, iid, now, cause, cfg=cfg):
            report.errored.append(iid)
        return
    report.adopted.append(iid)
    if state == State.BUSY:
        # old-process jobs are requeued without counting an attempt (restart is not their fault)
        report.requeued_jobs += machine.requeue_running_jobs(
            conn, now, instance_id=iid, not_worker=boot_id, count_attempt=False
        )
    if row["pool"] not in cfg.pools:
        return  # F-R2-3: a removed pool; the controller moves the row to STOPPING
    if state == State.IDLE:
        with tx(conn):
            conn.execute("UPDATE instances SET idle_since=? WHERE id=?", (now, iid))
    else:
        machine.settle_idle(conn, iid, now, cfg)


async def reconcile_on_start(
    conn: sqlite3.Connection,
    adapters: Mapping[str, ProviderAdapter],
    inference: Mapping[str, InferenceBackend],
    secret: bytes,
    now: float,
    cfg: Config,
    boot_id: str,
) -> ReconcileReport:
    assert_not_in_tx()
    with tx(conn):
        set_key(conn, "boot_id", boot_id, now)
    report = ReconcileReport()
    rows = conn.execute("SELECT * FROM instances WHERE state != 'STOPPED' ORDER BY id").fetchall()
    owned: dict[str, list[RemoteResource] | None] = {}
    for provider in sorted({r["provider"] for r in rows}):
        adapter = adapters.get(provider)
        if adapter is None:
            owned[provider] = None
            continue
        try:
            owned[provider] = await adapter.list_owned("llmhub-")
        except Exception as exc:  # noqa: BLE001 - adapters may raise anything
            owned[provider] = None
            record_incident(
                conn, now, "reconcile_list_failed", "high", ref=provider,
                dedup_key=f"reconcile_list:{provider}:{boot_id}",
                details={"error": adapter.normalize_error(exc).message[:200]},
            )  # fmt: skip
    for row in rows:
        listed = owned.get(row["provider"])
        backend = inference.get(row["provider"])
        if listed is None or backend is None:
            continue  # no adapter / failed list: nothing is decided (the controller reports)
        fresh = conn.execute("SELECT * FROM instances WHERE id=?", (row["id"],)).fetchone()
        await _reconcile_row(
            conn, adapters[row["provider"]], backend, fresh, listed, now, cfg, boot_id, report,
        )  # fmt: skip
    machine.error_to_stopping(conn, now)
    with tx(conn):
        report.requeued_jobs += machine.requeue_running_jobs(
            conn, now, not_worker=boot_id, count_attempt=False
        )
        leases.expire_due(conn, now)
    return report
