"""Kill switch: HALT + every live instance to STOPPING + destroy everything we own (plan 3.9).

Shared by the controller (admin kill, KILL file), and the reaper (KILL file, hard cap). Steps:
1. one transaction: HALT, close hold/prewarm leases and their reservations, live rows ->
   STOPPING (E15/E18, running jobs requeued without counting an attempt);
2. destroy every row in teardown and every listed resource whose HMAC label verifies;
3. poll until the provider lists zero of our resources, every `poll_s`, at most `deadline_s`;
   no zero by then -> critical incident `kill_incomplete`.
Kill never waits for a hanging create: its row is already STOPPING, and the late ref is
destroyed by `adopt_ref_or_destroy`. Foreign resources (no valid HMAC) are never touched.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from llmhub.budget_manager import reservations
from llmhub.clock import Clock
from llmhub.configuration.models import Config
from llmhub.lifecycle_manager import machine, teardown
from llmhub.lifecycle_manager.safety import halt, record_incident
from llmhub.persistence.db import assert_not_in_tx, tx
from llmhub.provider_adapters.base import ProviderAdapter, RemoteResource
from llmhub.provider_adapters.labels import verify_label

Actor = Literal["controller", "reaper", "admin"]
KILL_FILE = "KILL"


@dataclass
class KillReport:
    destroyed: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    stopped_rows: list[str] = field(default_factory=list)
    completed: bool = False
    elapsed_s: float = 0.0


def check_kill_file(data_dir: Path) -> bool:
    return (Path(data_dir) / KILL_FILE).exists()


def begin_kill(conn: sqlite3.Connection, now: float, actor: Actor, reason: str) -> list[str]:
    """Step 1 (DB only): HALT, close holds/prewarms, live rows -> STOPPING. Idempotent."""
    with tx(conn):
        halt(conn, reason, now)
        conn.execute(
            "UPDATE leases SET closed_at=?, close_reason=? WHERE kind IN ('hold','prewarm')"
            " AND closed_at IS NULL",
            (now, reason),
        )
        reservations.release_scopes(conn, ("hold", "prewarm"), now, reason)
    return machine.kill_to_stopping(conn, now, actor, reason)


async def _owned_ours(adapter: ProviderAdapter, secret: bytes) -> list[RemoteResource] | None:
    try:
        owned = await adapter.list_owned("llmhub-")
    except Exception:  # noqa: BLE001 - adapters may raise anything
        return None
    return [r for r in owned if verify_label(r.label, secret)]


async def kill_pass(
    conn: sqlite3.Connection,
    adapters: Mapping[str, ProviderAdapter],
    secret: bytes,
    now: float,
    cfg: Config,
    actor: Actor,
    report: KillReport | None = None,
) -> list[str]:
    """Step 2, one non-blocking pass. Returns refs of our resources still listed after it."""
    assert_not_in_tx()
    report = report or KillReport()
    for row in conn.execute(
        "SELECT * FROM instances WHERE state IN ('STOPPING','PENDING_DELETE','LEAKED') ORDER BY id"
    ).fetchall():
        adapter = adapters.get(row["provider"])
        if adapter is None:
            continue
        if row["state"] == "STOPPING":
            await teardown.request_destroy(conn, adapter, row, now, cfg, actor)
        else:
            await teardown.confirm_zero(conn, adapter, row, now, cfg, actor)
    remaining: list[str] = []
    for adapter in adapters.values():
        ours = await _owned_ours(adapter, secret)
        if ours is None:
            remaining.append(f"{adapter.name}:list_failed")
            continue
        for res in ours:
            try:
                await adapter.destroy(res.ref)
            except Exception:  # noqa: BLE001, S112 - retried on the next pass
                continue
            if res.ref not in report.destroyed:
                report.destroyed.append(res.ref)
        after = await _owned_ours(adapter, secret)
        if after is None:
            remaining.append(f"{adapter.name}:list_failed")
        else:
            remaining += [r.ref for r in after]
    return sorted(set(remaining))


async def kill_all(
    conn: sqlite3.Connection,
    adapters: Mapping[str, ProviderAdapter],
    secret: bytes,
    clock: Clock,
    cfg: Config,
    actor: Actor,
    deadline_s: float = 60,
    *,
    reason: str = "kill",
    poll_s: float = 2.0,
) -> KillReport:
    """HALT and destroy everything we own; poll to zero for at most `deadline_s`."""
    assert_not_in_tx()
    start = clock.now()
    report = KillReport(stopped_rows=begin_kill(conn, start, actor, reason))
    deadline = start + deadline_s
    while True:
        now = clock.now()
        report.remaining = await kill_pass(conn, adapters, secret, now, cfg, actor, report)
        if not report.remaining:
            report.completed = True
            break
        if now >= deadline:
            record_incident(
                conn, now, "kill_incomplete", "critical",
                dedup_key=f"kill_incomplete:{int(start)}",
                details={"remaining": report.remaining[:50], "actor": actor},
            )  # fmt: skip
            break
        await clock.sleep_until(min(now + poll_s, deadline))
    report.elapsed_s = clock.now() - start
    return report
