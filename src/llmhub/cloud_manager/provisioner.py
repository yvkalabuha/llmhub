"""Provisioning: create / adopt / advance PROVISIONING -> BOOTING -> LOADING_MODEL -> READY.

Rules (plan sections 3.4, 3.9, 7.3, gates 2026-11-04):
- the intent (`create_called_at`, `attempts+1`, `offer_json`) is written BEFORE `create`; the
  call itself runs as a background task (`spawn`), so a hanging create never blocks `step()`
  or a kill;
- a create answer is always stored (`adopt_ref_or_destroy`, no state CAS); a row that has left
  PROVISIONING destroys the late ref at once;
- unknown outcome: after `create_visibility_grace_s` the label is looked up in `list_owned`;
  found -> adopt (oldest wins, the rest are duplicates), not found -> retry with the SAME label
  until `create_max_attempts`, then E3;
- E2 right after create once `get(ref)` shows the resource (PENDING/RUNNING; the boot is
  then timed by BOOTING, E5). A row that keeps a ref without E2 (restart, ghost) polls `get`
  until RUNNING; a ghost ref (`get` = None while another resource carries the label) moves the
  row to that resource.
Provider calls never run inside a DB transaction.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Any

from llmhub.budget_manager.accounts import handle_provider_error
from llmhub.cloud_manager.instance_env import InstanceMaterial
from llmhub.cloud_manager.load_status import LoadStatus, LoadStatusSource
from llmhub.cloud_manager.offers import select_offer
from llmhub.cloud_manager.pools import PoolRuntime
from llmhub.configuration.models import Config
from llmhub.lifecycle_manager import machine, timeouts
from llmhub.lifecycle_manager.machine import LOAD_PROGRESS_PREFIX, LOAD_SLOW_PREFIX, transition
from llmhub.lifecycle_manager.recovery import destroy_duplicates, split_duplicates
from llmhub.lifecycle_manager.safety import (
    blacklist_host,
    delete_key,
    get_key,
    record_incident,
    set_key,
)
from llmhub.lifecycle_manager.states import State
from llmhub.lifecycle_manager.teardown import adopt_ref_or_destroy
from llmhub.persistence.db import assert_not_in_tx, tx
from llmhub.provider_adapters.base import (
    InstanceSpec,
    NormalizedError,
    Offer,
    OfferQuery,
    ProviderAdapter,
    ProviderError,
    RemoteResource,
    RemoteStatus,
    UnavailableReason,
)

Spawn = Callable[[str, Coroutine[Any, Any, Any]], None]


class CreateOutcome(enum.StrEnum):
    CREATED = "created"
    ADOPTED = "adopted"
    RETRY_LATER = "retry_later"
    NO_OFFER = "no_offer"
    BLOCKED = "blocked"
    FAILED = "failed"
    SPAWNED = "spawned"  # the create call runs in the background


_CIRCUIT_OPEN = "circuit open"


def find_duplicates(owned: list[RemoteResource], inst: sqlite3.Row) -> list[RemoteResource]:
    """Resources carrying the row's label that are not the row's resource (section 3.9).

    With `provider_ref` set and listed: every other resource with the label. With no ref (or a
    ghost ref not listed): all but the oldest (`created_at`, then ref).
    """
    mine = [r for r in owned if r.label == inst["label"]]
    known = next((r for r in mine if r.ref == inst["provider_ref"]), None)
    return split_duplicates(inst, mine, known)[1]


def _exclude_key(instance_id: str) -> str:
    return f"exclude_hosts:{instance_id}"


def _excluded(conn: sqlite3.Connection, instance_id: str) -> frozenset[str]:
    raw = get_key(conn, _exclude_key(instance_id))
    return frozenset(json.loads(raw)) if raw else frozenset()


def blacklisted_hosts(conn: sqlite3.Connection, provider: str, now: float) -> frozenset[str]:
    rows = conn.execute(
        "SELECT host_id FROM host_blacklist WHERE provider=? AND until>?", (provider, now)
    ).fetchall()
    return frozenset(str(r["host_id"]) for r in rows)


def _breaker_open(adapter: ProviderAdapter) -> bool:
    breaker = getattr(adapter, "breaker", None)
    if breaker is None:
        return False
    return bool(breaker.state == "open" and breaker.retry_in() > 0)


def _fail_provisioning(conn: sqlite3.Connection, iid: str, now: float, cause: str) -> bool:
    with tx(conn):
        return transition(conn, iid, State.PROVISIONING, State.ERROR, "E3", now, "controller",
                          last_error=cause, reason=cause)  # fmt: skip


async def _do_create(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    spec: InstanceSpec,
    host_id: str,
    cfg: Config,
    now_fn: Callable[[], float],
) -> CreateOutcome:
    """The create call itself (background). Outcome handling per section 7.3."""
    assert_not_in_tx()
    iid = inst["id"]
    try:
        res = await adapter.create(spec, inst["label"])
    except Exception as exc:  # noqa: BLE001 - adapters may raise anything
        err = exc.err if isinstance(exc, ProviderError) else adapter.normalize_error(exc)
        return _create_failed(conn, inst, err, host_id, cfg, now_fn())
    if not await adopt_ref_or_destroy(conn, adapter, iid, res, now_fn()):
        return CreateOutcome.FAILED
    try:  # a ghost ref is not visible: the PROVISIONING step moves the row to the label
        seen = await adapter.get(res.ref)
    except Exception:  # noqa: BLE001 - the PROVISIONING step polls again
        seen = None
    if seen is not None and seen.status in (RemoteStatus.PENDING, RemoteStatus.RUNNING):
        with tx(conn):
            transition(conn, iid, State.PROVISIONING, State.BOOTING, "E2", now_fn(),
                       "controller", endpoint=seen.endpoint)  # fmt: skip
            delete_key(conn, _exclude_key(iid))
    return CreateOutcome.CREATED


def _create_failed(
    conn: sqlite3.Connection,
    inst: sqlite3.Row,
    err: NormalizedError,
    host_id: str,
    cfg: Config,
    now: float,
) -> CreateOutcome:
    iid = inst["id"]
    msg = err.message[:200]
    if err.outcome_unknown:  # resolved by the label lookup after the grace period
        with tx(conn):
            conn.execute(
                "UPDATE instances SET last_error=?, create_unknown=1 WHERE id=?", (msg, iid)
            )
        return CreateOutcome.RETRY_LATER
    if handle_provider_error(conn, inst["provider"], err, now):  # balance / quota: no retries
        _fail_provisioning(conn, iid, now, str(err.reason))
        return CreateOutcome.BLOCKED
    refund = err.message == _CIRCUIT_OPEN
    with tx(conn):
        if err.reason == UnavailableReason.GPU_UNAVAILABLE:
            hosts = sorted(_excluded(conn, iid) | {host_id})
            set_key(conn, _exclude_key(iid), json.dumps(hosts), now)
            refund = True  # the next offer is a new try, not a repeated one
        conn.execute(
            "UPDATE instances SET create_called_at=NULL, last_error=?,"
            " attempts=CASE WHEN ? THEN max(attempts-1, 0) ELSE attempts END WHERE id=?",
            (msg, int(refund), iid),
        )
        attempts = int(
            conn.execute("SELECT attempts FROM instances WHERE id=?", (iid,)).fetchone()[0]
        )
    if not refund and attempts >= cfg.retry.create_max_attempts:
        _fail_provisioning(conn, iid, now, "create_failed")
        return CreateOutcome.FAILED
    return CreateOutcome.RETRY_LATER


async def _start_create(
    conn: sqlite3.Connection,
    rt: PoolRuntime,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    spawn: Spawn | None,
    now_fn: Callable[[], float],
) -> CreateOutcome:
    pool = rt.cfg
    adapter = rt.adapter
    if _breaker_open(adapter):
        return CreateOutcome.RETRY_LATER
    query = OfferQuery(
        gpu=pool.gpu, min_vram_gb=pool.vram_gb, max_price_usd_h=pool.max_price_usd_h,
        exclude_hosts=_excluded(conn, inst["id"]),
        need_public_ip=pool.infer.transport == "direct",  # direct TLS needs a reachable IP
    )  # fmt: skip
    try:
        offers = await adapter.list_offers(query)
    except Exception:  # noqa: BLE001 - adapters may raise anything; retried next step
        offers = []
    material: InstanceMaterial | None = None
    if rt.material is not None:
        try:  # derived from the row: a repeated create with the same label gets the same
            material = rt.material(inst)
        except Exception as exc:  # noqa: BLE001 - a config problem must not stop the loop
            record_incident(
                conn, now, "instance_material_failed", "high", ref=inst["id"],
                dedup_key=f"instance_material:{inst['id']}",
                details={"error": type(exc).__name__},
            )  # fmt: skip
            _fail_provisioning(conn, inst["id"], now, "instance_material")
            return CreateOutcome.FAILED
    offer: Offer | None = select_offer(offers, query, blacklisted_hosts(conn, pool.provider, now))
    if offer is None:
        if now >= float(inst["state_since"]) + cfg.timeouts.offer_wait_s:
            _fail_provisioning(conn, inst["id"], now, str(UnavailableReason.GPU_UNAVAILABLE))
            return CreateOutcome.FAILED
        return CreateOutcome.NO_OFFER
    offer_json = json.dumps(
        {"offer_id": offer.offer_id, "host_id": offer.host_id, "price_usd_h": offer.price_usd_h,
         "disk_usd_h": offer.disk_usd_h},
        sort_keys=True,
    )  # fmt: skip
    with tx(conn):  # the intent is in the DB before the call
        cur = conn.execute(
            "UPDATE instances SET offer_json=?, create_called_at=?, attempts=attempts+1,"
            " first_create_called_at=COALESCE(first_create_called_at, ?),"
            " last_create_called_at=?, gpu=?, tls_fingerprint=?"
            " WHERE id=? AND state='PROVISIONING' AND create_called_at IS NULL",
            (offer_json, now, now, now, offer.gpu,
             material.tls_fingerprint if material is not None else None, inst["id"]),
        )  # fmt: skip
        if cur.rowcount != 1:
            return CreateOutcome.RETRY_LATER
    spec = InstanceSpec(
        pool=pool.name, offer_id=offer.offer_id, image=pool.image, disk_gb=pool.disk_gb,
        max_price_usd_h=pool.max_price_usd_h, max_lifetime_s=cfg.timeouts.max_lifetime_s,
        env=dict(material.env) if material is not None else {},
        secret_env=dict(material.secret_env) if material is not None else {},
    )  # fmt: skip
    coro = _do_create(conn, adapter, inst, spec, offer.host_id, cfg, now_fn)
    if spawn is None:
        return await coro
    spawn(inst["id"], coro)
    return CreateOutcome.SPAWNED


async def create_or_adopt(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    spec: InstanceSpec,
    now: float,
    cfg: Config,
) -> CreateOutcome:
    """Inline variant (tests, tools): record the intent, call create, adopt the answer."""
    assert_not_in_tx()
    with tx(conn):
        conn.execute(
            "UPDATE instances SET create_called_at=?, attempts=attempts+1,"
            " first_create_called_at=COALESCE(first_create_called_at, ?),"
            " last_create_called_at=? WHERE id=?",
            (now, now, now, inst["id"]),
        )
    return await _do_create(conn, adapter, inst, spec, "", cfg, lambda: now)


async def _adopt_by_label(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    now: float,
) -> RemoteResource | None:
    """Look the label up; adopt the oldest resource, destroy the other ones (duplicates)."""
    owned = [r for r in await adapter.list_owned(inst["label"]) if r.label == inst["label"]]
    owned = [r for r in owned if r.status not in (RemoteStatus.TERMINATED, RemoteStatus.EXITED)]
    target, extras = split_duplicates(inst, owned, None)
    if target is None:
        return None
    if extras:
        await destroy_duplicates(conn, adapter, inst, extras, now)
    if inst["provider_ref"] is not None and inst["provider_ref"] != target.ref:
        record_incident(
            conn, now, "ghost_ref", "high", ref=inst["id"],
            dedup_key=f"ghost:{inst['provider_ref']}",
            details={"ghost_ref": inst["provider_ref"], "adopted_ref": target.ref},
        )  # fmt: skip
    if not await adopt_ref_or_destroy(conn, adapter, inst["id"], target, now):
        return None
    return target


async def _provisioning(
    conn: sqlite3.Connection,
    rt: PoolRuntime,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    spawn: Spawn | None,
    now_fn: Callable[[], float],
) -> None:
    adapter = rt.adapter
    iid = inst["id"]
    grace_end = (
        float(inst["create_called_at"]) + cfg.timeouts.create_visibility_grace_s
        if inst["create_called_at"] is not None
        else now
    )
    if inst["provider_ref"] is not None:
        res = await adapter.get(inst["provider_ref"])
        if res is None or res.status in (RemoteStatus.TERMINATED, RemoteStatus.EXITED):
            moved = await _adopt_by_label(conn, adapter, inst, now)  # ghost ref
            if moved is None:
                if now >= grace_end:
                    machine.mark_lost(conn, iid, now, "resource_gone", cfg=cfg)
                return
            res = moved
        if res.status == RemoteStatus.RUNNING:  # adopted after a restart: poll until RUNNING
            with tx(conn):
                transition(conn, iid, State.PROVISIONING, State.BOOTING, "E2", now, "controller",
                           endpoint=res.endpoint)  # fmt: skip
                delete_key(conn, _exclude_key(iid))
        return
    if inst["create_called_at"] is not None:  # unknown outcome or a restart during create
        found = await _adopt_by_label(conn, adapter, inst, now)
        if found is not None or now < grace_end:
            return
        if int(inst["attempts"]) >= cfg.retry.create_max_attempts:
            _fail_provisioning(conn, iid, now, "create_unknown_max_attempts")
            return
        with tx(conn):  # retry with the same label (the outcome of the last call is unknown)
            conn.execute(
                "UPDATE instances SET create_called_at=NULL, create_unknown=1"
                " WHERE id=? AND provider_ref IS NULL",
                (iid,),
            )
        fresh = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
        if fresh is None or fresh["provider_ref"] is not None:
            return
        inst = fresh
    await _start_create(conn, rt, inst, now, cfg, spawn, now_fn)


LLAMA_LOAD_S = 60.0  # model load after the download (ETA margin, plan 2a item 6)
SLOW_MIN_S = 60.0  # no early E7 during the first minute of LOADING_MODEL
# F3 (fix round 1): "too slow" must hold for SLOW_CONFIRM_S (longer than the pod's 10 s speed
# window), and the ETA is taken from the AVERAGE speed since LOADING_MODEL started whenever the
# pod reports byte counts, so a short dip (chunk retry, CDN pause) cannot kill a pod that fits.
SLOW_CONFIRM_S = 30.0
_DOWNLOAD_PHASES = frozenset({"downloading", "verifying", "loading"})
# F6: pod failure reasons that point at our config/sources, not at the host -> no blacklist
_CONFIG_REASONS = frozenset({"exhausted", "sha_mismatch", "size_mismatch"})


def _progress_key(iid: str) -> str:
    return f"{LOAD_PROGRESS_PREFIX}{iid}"


def _slow_key(iid: str) -> str:
    return f"{LOAD_SLOW_PREFIX}{iid}"


def _clear_load_keys(conn: sqlite3.Connection, iid: str) -> None:
    delete_key(conn, _progress_key(iid))
    delete_key(conn, _slow_key(iid))


def _weights_error(conn: sqlite3.Connection, inst: sqlite3.Row, now: float, cfg: Config,
                   cause: str, *, host_fault: bool = True) -> None:  # fmt: skip
    """Early E7 (LOADING_MODEL -> ERROR) without waiting for `loading_model_s`.

    `host_fault=False` (config/source errors, F6): no host blacklist, a high incident instead.
    """
    with tx(conn):
        if transition(conn, inst["id"], State.LOADING_MODEL, State.ERROR, "E7", now,
                      "controller", last_error=cause, reason=cause):  # fmt: skip
            if host_fault:
                blacklist_host(conn, inst["provider"], inst["host_id"], now,
                               cfg.retry.host_blacklist_s, cause)  # fmt: skip
            else:
                record_incident(
                    conn, now, "weights_config", "high", ref=inst["id"],
                    dedup_key=f"weights_config:{inst['pool']}:{int(now // 3600)}",
                    details={"cause": cause},
                )  # fmt: skip
            _clear_load_keys(conn, inst["id"])


def _eta(st: LoadStatus, inst: sqlite3.Row, now: float) -> float | None:
    """Remaining download time: average speed since LOADING_MODEL started (byte counts known),
    else the pod's own ETA. None = unknown."""
    elapsed = now - float(inst["state_since"])
    if st.total > 0:
        if st.done <= 0 or elapsed <= 0:
            return None  # nothing measured yet
        return max(0, st.total - st.done) / (st.done / elapsed)
    return st.eta_s


def _too_slow(st: LoadStatus, inst: sqlite3.Row, now: float, cfg: Config) -> bool:
    """No fallback source left and the projected finish misses the LOADING_MODEL budget."""
    since = float(inst["state_since"])
    if now - since < SLOW_MIN_S or st.status != "loading" or st.phase not in _DOWNLOAD_PHASES:
        return False
    if st.sources_left != 0:  # a fallback exists
        return False
    eta = _eta(st, inst, now)
    if eta is None:
        return False
    return now + eta + LLAMA_LOAD_S > since + cfg.timeouts.loading_model_s


async def _load_progress(
    conn: sqlite3.Connection, rt: PoolRuntime, inst: sqlite3.Row, now: float, cfg: Config
) -> bool:
    """Read the pod's load status (optional `LoadStatusSource`). True = this step is done.

    `failed` -> E7 `weights_failed:<reason>` at once; still downloading and too slow for
    `SLOW_CONFIRM_S` without a break -> E7 `weights_too_slow`; `None` (unknown/not ours) or a
    backend without `load_status` -> the old health/test path.
    """
    backend = rt.inference
    if not isinstance(backend, LoadStatusSource):
        return False
    iid = inst["id"]
    try:
        st = await backend.load_status(inst["endpoint"])
    except Exception:  # noqa: BLE001 - backends may raise anything; poll again later
        st = None
    if st is None:
        return False
    if st.status == "failed":
        reason = st.reason or "unknown"
        _weights_error(conn, inst, now, cfg, f"weights_failed:{reason}"[:80],
                       host_fault=reason not in _CONFIG_REASONS)  # fmt: skip
        return True
    slow = _too_slow(st, inst, now, cfg)
    raw = get_key(conn, _slow_key(iid))
    slow_since = (float(raw) if raw is not None else now) if slow else None
    with tx(conn):
        set_key(conn, _progress_key(iid), json.dumps(st.to_json_dict(), sort_keys=True), now)
        if slow_since is None:
            delete_key(conn, _slow_key(iid))
        elif raw is None:
            set_key(conn, _slow_key(iid), repr(now), now)
    if slow_since is not None and now - slow_since >= SLOW_CONFIRM_S:
        _weights_error(conn, inst, now, cfg, "weights_too_slow")
        return True
    return st.status == "loading" and st.phase in _DOWNLOAD_PHASES


async def provision_step(
    conn: sqlite3.Connection,
    pool_rt: PoolRuntime,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    secret: bytes,
    *,
    spawn: Spawn | None = None,
    now_fn: Callable[[], float] | None = None,
) -> None:
    """One step of PROVISIONING / BOOTING / LOADING_MODEL; at most one transition.

    `spawn(instance_id, coro)` runs the create call in the background (the controller passes
    it); without it the call is awaited inline. Timeouts (E3/E5/E7) are applied by
    `timeouts.apply_timeouts`, liveness by `timeouts.check_alive`.
    """
    _ = secret  # labels are made at insert time; kept for the plan signature
    state = State(inst["state"])
    clock_now = now_fn or (lambda: now)
    try:
        if state == State.PROVISIONING:
            await _provisioning(conn, pool_rt, inst, now, cfg, spawn, clock_now)
        elif state == State.BOOTING:
            assert inst["endpoint"] is not None
            if await pool_rt.inference.ping(inst["endpoint"]):
                with tx(conn):
                    transition(conn, inst["id"], State.BOOTING, State.LOADING_MODEL, "E4", now,
                               "controller")  # fmt: skip
        elif state == State.LOADING_MODEL:
            assert inst["endpoint"] is not None
            ep = inst["endpoint"]
            if await _load_progress(conn, pool_rt, inst, now, cfg):
                return  # E7 done, or the pod still downloads (health would be 503 anyway)
            if not await pool_rt.inference.health(ep):
                return  # still loading; the 20 min timeout is E7 in apply_timeouts
            if await pool_rt.inference.test_request(ep):
                with tx(conn):
                    if transition(conn, inst["id"], State.LOADING_MODEL, State.READY, "E6", now,
                                  "controller", ready_at=now):  # fmt: skip
                        _clear_load_keys(conn, inst["id"])
            else:
                timeouts.note_model_test_failure(conn, inst["id"], now, cfg)
    except ProviderError:
        return  # reads are retried by GuardedAdapter; still failing -> next step
