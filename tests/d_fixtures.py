"""Shared helpers for stream D tests (plain DB + BodyStore, no controller)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from harness import CLIENT_ID, default_config, make_job
from llmhub.configuration.models import Config
from llmhub.persistence.db import connect, migrate
from llmhub.task_queue.bodies import BodyStore
from llmhub.task_queue.models import JobSpec, Priority

T0 = 1_793_750_400.0

__all__ = ["CLIENT_ID", "T0", "Env", "make_job", "spec"]


class Env:
    def __init__(self, tmp: Path, busy_ms: int = 300) -> None:
        self.cfg: Config = default_config(tmp)
        self.path = tmp / "state.db"
        self.conn = connect(self.path, busy_timeout_ms=busy_ms)
        migrate(self.conn)
        for cid in (CLIENT_ID, "c2", "c3"):
            self.conn.execute(
                "INSERT INTO clients(id,name,key_hash,created_at,data_class)"
                " VALUES (?,?,?,0,'cloud_allowed')",
                (cid, cid, "x"),
            )
        self.conn.execute(
            "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
            " created_at, max_lifetime_at) VALUES ('i1','op1','p1','mock','l1','BUSY',0,0,0)"
        )
        self.bodies = BodyStore(tmp / "bodies")

    def job(self, job_id: str) -> sqlite3.Row:
        row: sqlite3.Row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row

    def lease(self, job_id: str) -> sqlite3.Row:
        row: sqlite3.Row = self.conn.execute(
            "SELECT * FROM leases WHERE job_id=?", (job_id,)
        ).fetchone()
        return row


def spec(n: int = 0, *, client: str = CLIENT_ID, prio: Priority = Priority.NORMAL,
         key: str | None = None, body: bytes | None = None) -> JobSpec:  # fmt: skip
    base = make_job(n, priority=prio)
    return JobSpec(
        client_id=client, body=body if body is not None else base.body, priority=prio,
        model_alias=base.model_alias, policy=base.policy, data_class=base.data_class,
        route=base.route, pool=base.pool, route_reason=base.route_reason, idem_key=key,
        est_tokens_in=base.est_tokens_in, max_tokens=base.max_tokens,
    )  # fmt: skip
