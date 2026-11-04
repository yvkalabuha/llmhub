"""Destroy + zero confirmation, shared by controller and reaper (S0: happy path)."""

from __future__ import annotations

import sqlite3
from typing import Literal

from llmhub.configuration.models import Config
from llmhub.lifecycle_manager.machine import transition
from llmhub.lifecycle_manager.states import State
from llmhub.persistence.db import assert_not_in_tx, tx
from llmhub.provider_adapters.base import ProviderAdapter


async def request_destroy(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    actor: Literal["controller", "reaper", "admin"],
) -> None:
    """STOPPING: record the intent, call destroy; ACCEPTED/ALREADY_GONE -> PENDING_DELETE (E19)."""
    assert_not_in_tx()
    if inst["destroy_requested_at"] is None:
        with tx(conn):
            conn.execute(
                "UPDATE instances SET destroy_requested_at=? WHERE id=?", (now, inst["id"])
            )
    ref = inst["provider_ref"]
    if ref is not None:
        await adapter.destroy(ref)
    with tx(conn):
        transition(conn, inst["id"], State.STOPPING, State.PENDING_DELETE, "E19", now, actor,
                   destroy_accepted_at=now)  # fmt: skip


async def confirm_zero(
    conn: sqlite3.Connection,
    adapter: ProviderAdapter,
    inst: sqlite3.Row,
    now: float,
    cfg: Config,
    actor: Literal["controller", "reaper", "admin"],
) -> bool:
    """PENDING_DELETE/LEAKED -> STOPPED when no resource with the label remains."""
    assert_not_in_tx()
    owned = [r for r in await adapter.list_owned(inst["label"]) if r.label == inst["label"]]
    if owned:
        return False
    src = State(inst["state"])
    trigger = "E22" if src == State.LEAKED else "E20"
    with tx(conn):
        return transition(conn, inst["id"], src, State.STOPPED, trigger, now, actor,
                          zero_confirmed_at=now)  # fmt: skip
