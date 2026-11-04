"""Provisioning: create / adopt / advance PROVISIONING -> READY (S0: happy path only)."""

from __future__ import annotations

import enum
import json
import sqlite3

from llmhub.cloud_manager.offers import select_offer
from llmhub.cloud_manager.pools import PoolRuntime
from llmhub.configuration.models import Config
from llmhub.lifecycle_manager.machine import set_provider_ref, transition
from llmhub.lifecycle_manager.states import State
from llmhub.persistence.db import assert_not_in_tx, tx
from llmhub.provider_adapters.base import (
    InstanceSpec,
    OfferQuery,
    ProviderAdapter,
    RemoteResource,
)


class CreateOutcome(enum.StrEnum):
    CREATED = "created"
    ADOPTED = "adopted"
    RETRY_LATER = "retry_later"
    NO_OFFER = "no_offer"
    BLOCKED = "blocked"
    FAILED = "failed"


async def create_or_adopt(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    spec: InstanceSpec,
    now: float,
    cfg: Config,
) -> CreateOutcome:
    """S0: the intent is recorded first, then create is called outside any transaction."""
    assert_not_in_tx()
    with tx(conn):
        conn.execute(
            "UPDATE instances SET create_called_at=?, attempts=attempts+1 WHERE id=?",
            (now, inst["id"]),
        )
    res = await adapter.create(spec, inst["label"])
    state = set_provider_ref(conn, inst["id"], res.ref, res.host_id, res.price_usd_h, now)
    if state != State.PROVISIONING:
        await adapter.destroy(res.ref)
        return CreateOutcome.FAILED
    with tx(conn):
        transition(conn, inst["id"], State.PROVISIONING, State.BOOTING, "E2", now, "controller",
                   endpoint=res.endpoint)  # fmt: skip
    return CreateOutcome.CREATED


def find_duplicates(owned: list[RemoteResource], inst: sqlite3.Row) -> list[RemoteResource]:
    raise NotImplementedError("stream F (F2)")


async def provision_step(
    conn: sqlite3.Connection,
    pool_rt: PoolRuntime,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    secret: bytes,
) -> None:
    """One step of PROVISIONING / BOOTING / LOADING_MODEL. At most one transition."""
    state = State(inst["state"])
    pool = pool_rt.cfg
    if state == State.PROVISIONING and inst["create_called_at"] is None:
        query = OfferQuery(
            gpu=pool.gpu, min_vram_gb=pool.vram_gb, max_price_usd_h=pool.max_price_usd_h,
            exclude_hosts=frozenset(),
        )  # fmt: skip
        offer = select_offer(await pool_rt.adapter.list_offers(query), query, ())
        if offer is None:
            return  # stream F: offer_wait / gpu_unavailable handling
        with tx(conn):
            conn.execute(
                "UPDATE instances SET offer_json=? WHERE id=?",
                (json.dumps({"offer_id": offer.offer_id, "host_id": offer.host_id}), inst["id"]),
            )
        spec = InstanceSpec(
            pool=pool.name, offer_id=offer.offer_id, image=pool.image, disk_gb=pool.disk_gb,
            max_price_usd_h=pool.max_price_usd_h, max_lifetime_s=cfg.timeouts.max_lifetime_s,
            env={},
        )  # fmt: skip
        await create_or_adopt(conn, pool_rt.adapter, inst, spec, now, cfg)
    elif state == State.BOOTING:
        assert inst["endpoint"] is not None
        if await pool_rt.inference.ping(inst["endpoint"]):
            with tx(conn):
                transition(conn, inst["id"], State.BOOTING, State.LOADING_MODEL, "E4", now,
                           "controller")  # fmt: skip
    elif state == State.LOADING_MODEL:
        assert inst["endpoint"] is not None
        ep = inst["endpoint"]
        if await pool_rt.inference.health(ep) and await pool_rt.inference.test_request(ep):
            with tx(conn):
                transition(conn, inst["id"], State.LOADING_MODEL, State.READY, "E6", now,
                           "controller", ready_at=now)  # fmt: skip
