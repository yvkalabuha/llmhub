"""State timeouts, max_lifetime, liveness and model-test failures (plan section 5).

`apply_timeouts` is pure DB work (sync, one transaction). `check_alive` talks to the provider
and the inference backend, so it runs outside any transaction.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from llmhub.cloud_manager.inference import InferenceBackend
from llmhub.configuration.models import Config
from llmhub.lifecycle_manager import machine
from llmhub.lifecycle_manager.machine import Actor
from llmhub.lifecycle_manager.safety import blacklist_host
from llmhub.lifecycle_manager.states import LIVE_IN_INDEX, State
from llmhub.persistence.db import assert_not_in_tx, tx
from llmhub.provider_adapters.base import ProviderAdapter, RemoteStatus

# state -> (edge to ERROR, timeout attribute of TimeoutsConfig)
_STATE_TIMEOUT = {
    State.PROVISIONING: ("E3", "provisioning_s"),
    State.BOOTING: ("E5", "booting_s"),
    State.LOADING_MODEL: ("E7", "loading_model_s"),
}


def apply_timeouts(
    conn: sqlite3.Connection, now: float, cfg: Config, actor: Actor = "controller"
) -> list[tuple[str, str]]:
    """State timeouts (E3/E5/E7) and max_lifetime (E15/E16). Returns (instance_id, edge) pairs.

    max_lifetime always wins over leases and the queue: READY/IDLE/BOOTING/... go to STOPPING
    at once (E15), BUSY finishes its running jobs first (`stop_after_current`, then E16).
    """
    out: list[tuple[str, str]] = []
    marks = ",".join("?" for _ in LIVE_IN_INDEX)
    with tx(conn):
        rows = conn.execute(
            f"SELECT * FROM instances WHERE state IN ({marks}) ORDER BY id", sorted(LIVE_IN_INDEX)
        ).fetchall()
        for r in rows:
            state = State(r["state"])
            if now >= float(r["max_lifetime_at"]) and state not in (State.ERROR, State.STOPPING):
                if state == State.BUSY:
                    if not r["stop_after_current"]:
                        machine.request_stop_after_current(conn, r["id"], "max_lifetime")
                        out.append((r["id"], "stop_after_current"))
                elif machine.transition(conn, r["id"], state, State.STOPPING, "E15", now, actor,
                                        stop_reason="max_lifetime", reason="max_lifetime"):  # fmt: skip
                    machine.requeue_running_jobs(conn, now, instance_id=r["id"],
                                                 count_attempt=False)  # fmt: skip
                    out.append((r["id"], "E15"))
                continue
            spec = _STATE_TIMEOUT.get(state)
            if spec is None:
                continue
            edge, attr = spec
            limit = float(getattr(cfg.timeouts, attr))
            if now >= float(r["state_since"]) + limit:
                reason = f"{state.value.lower()}_timeout"
                if machine.transition(conn, r["id"], state, State.ERROR, edge, now, actor,
                                      last_error=reason, reason=reason):  # fmt: skip
                    if state != State.PROVISIONING:
                        blacklist_host(conn, r["provider"], r["host_id"], now,
                                       cfg.retry.host_blacklist_s, reason)  # fmt: skip
                    out.append((r["id"], edge))
        # BUSY with stop_after_current and no running jobs -> E16 is handled by settle_idle
    return out


def note_model_test_failure(
    conn: sqlite3.Connection, instance_id: str, now: float, cfg: Config
) -> bool:
    """Record a failed `test_request`; E7 when `model_test_retries` failures fall in the window."""
    with tx(conn):
        inst = conn.execute("SELECT * FROM instances WHERE id=?", (instance_id,)).fetchone()
        if inst is None or inst["state"] != State.LOADING_MODEL:
            return False
        window = cfg.timeouts.model_test_window_s
        fails = [t for t in json.loads(inst["model_test_failures_json"]) if now - t < window]
        fails.append(now)
        conn.execute(
            "UPDATE instances SET model_test_failures_json=? WHERE id=?",
            (json.dumps(fails), instance_id),
        )
        if len(fails) < cfg.timeouts.model_test_retries:
            return False
        ok = machine.transition(conn, instance_id, State.LOADING_MODEL, State.ERROR, "E7", now,
                                "controller", last_error="model_test_failed",
                                reason="model_test_failed")  # fmt: skip
        if ok:
            blacklist_host(conn, inst["provider"], inst["host_id"], now,
                           cfg.retry.host_blacklist_s, "model_test_failed")  # fmt: skip
        return ok


async def check_alive(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inference: InferenceBackend,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    actor: Actor = "controller",
) -> Literal["ok", "lost", "network_loss", "skipped"]:
    """Provider-side liveness of one instance with a resource.

    `get` = None/TERMINATED -> lost; EXITED -> `stopped_but_billing`; unreachable endpoint
    for `network_loss_s` -> network_loss. API errors on `get` change nothing (retry later).
    """
    assert_not_in_tx()
    state = State(inst["state"])
    ref = inst["provider_ref"]
    if ref is None or state not in (
        State.BOOTING, State.LOADING_MODEL, State.READY, State.BUSY, State.IDLE
    ):  # fmt: skip
        return "skipped"
    try:
        res = await adapter.get(ref)
    except Exception:  # noqa: BLE001 - adapters may raise anything
        return "skipped"
    if res is None or res.status == RemoteStatus.TERMINATED:
        machine.mark_lost(conn, inst["id"], now, "resource_gone", actor, cfg=cfg)
        return "lost"
    if res.status == RemoteStatus.EXITED:
        machine.mark_lost(conn, inst["id"], now, "stopped_but_billing", actor, cfg=cfg)
        return "lost"
    if state not in (State.READY, State.BUSY, State.IDLE) or inst["endpoint"] is None:
        return "ok"
    try:
        reachable = await inference.ping(inst["endpoint"])
    except Exception:  # noqa: BLE001 - adapters may raise anything
        reachable = False
    with tx(conn):
        if reachable:
            if inst["net_fail_since"] is not None:
                conn.execute("UPDATE instances SET net_fail_since=NULL WHERE id=?", (inst["id"],))
            return "ok"
        since = inst["net_fail_since"]
        if since is None:
            conn.execute("UPDATE instances SET net_fail_since=? WHERE id=?", (now, inst["id"]))
            return "ok"
    if now - float(since) > cfg.timeouts.network_loss_s:
        machine.mark_lost(conn, inst["id"], now, "network_loss", actor, cfg=cfg)
        return "network_loss"
    return "ok"
