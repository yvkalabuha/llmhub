"""T-372 L2b (2026-11-05): the `_probe` row after a definite refusal, old probe rows, and the
shared REST limiter state around `lifecycle-probe` (RunPod fake, no network)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from llmhub.configuration.models import PROBE_POOL
from llmhub.persistence.db import connect, migrate, tx
from llmhub.provider_adapters import rate_limit
from llmhub.provider_adapters.runpod import RunpodAdapter
from llmhub.provider_adapters.runpod_api import RunpodApi
from llmhub.tools.t372_ops import OpsError, lifecycle_probe
from runpod_fake import FAKE_KEY, FakeRunpod, runpod_cfg
from test_review_t372_fn_runpod_real import SMALL, _AutoClock, _cfg

G = "NVIDIA GeForce RTX 3090"
SECRET = b"x" * 32


def _db(tmp: Path) -> sqlite3.Connection:
    conn = connect(tmp / "s.db")
    migrate(conn)
    return conn


def _adapter(fake: FakeRunpod, clock: _AutoClock, *, limiter: bool = False,
             **cfg: Any) -> RunpodAdapter:  # fmt: skip
    lim = rate_limit.RestLimiter(clock.now) if limiter else None
    api = RunpodApi(lambda: FAKE_KEY, transport=fake.transport(), limiter=lim)
    return RunpodAdapter(api, runpod_cfg(list_staleness_s=0, **cfg), clock, disk_gb_hint=20)


async def _probe(conn: sqlite3.Connection, adapter: RunpodAdapter, clock: _AutoClock,
                 tmp: Path, **kw: Any) -> Any:  # fmt: skip
    return await lifecycle_probe(conn, adapter, SECRET, clock, _cfg(tmp), image=SMALL,
                                 gpu_type_id=G, max_s=60, poll_s=5, **kw)  # fmt: skip


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM instances WHERE pool=? ORDER BY created_at",
                        (PROBE_POOL,)).fetchall()  # fmt: skip


def _path(conn: sqlite3.Connection, iid: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT from_state, to_state FROM transitions WHERE instance_id=? ORDER BY id", (iid,))]


# ------------------------------------------------------------------ definite refusal
async def test_no_capacity_stops_row_at_once_and_probe_repeats(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock, cuda_capacity=set())
    conn = _db(tmp_path)
    adapter = _adapter(fake, clock)
    rep = await _probe(conn, adapter, clock, tmp_path)
    assert rep.error is not None and rep.ref is None and rep.zero
    (row,) = _rows(conn)
    assert row["state"] == "STOPPED" and row["zero_confirmed_at"] is not None
    assert _path(conn, row["id"]) == [("STOPPED", "PROVISIONING"), ("PROVISIONING", "ERROR"),
                                      ("ERROR", "STOPPING"), ("STOPPING", "PENDING_DELETE"),
                                      ("PENDING_DELETE", "STOPPED")]  # fmt: skip
    # no label listing after a definite refusal (REST budget): only GraphQL + the pre-check
    gets_after = [r for r in fake.requests if r.method == "GET" and r.url.path == "/v1/pods"]
    assert len(gets_after) == 1  # the max_live_pods count before the POSTs
    # the next probe starts right away (no UNIQUE(pool) error)
    fake.cuda_capacity = None
    rep2 = await _probe(conn, adapter, clock, tmp_path, hold_s=0)
    assert rep2.ref is not None and rep2.zero and fake.live_pods() == []
    assert [r["state"] for r in _rows(conn)] == ["STOPPED", "ERROR"]


async def test_no_stock_refusal_also_stops_row(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock, gpus={G: 0.22, "NVIDIA RTX A2000": 0.12},
                      community_stock={"NVIDIA RTX A2000": "Low"})  # fmt: skip
    conn = _db(tmp_path)
    rep = await _probe(conn, _adapter(fake, clock), clock, tmp_path)
    assert fake.posts() == 0 and rep.attempts == [(G, "", "no_stock")]
    assert rep.zero and _rows(conn)[0]["state"] == "STOPPED"


async def test_unknown_outcome_stays_strict(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock)
    fake.inject("POST", "/pods", "read_timeout")  # sent, no effect, no answer
    conn = _db(tmp_path)
    rep = await _probe(conn, _adapter(fake, clock), clock, tmp_path)
    (row,) = _rows(conn)
    assert row["state"] == "ERROR" and row["create_unknown"] == 1 and rep.ref is None
    # an unknown outcome is never cleared by the next probe
    clock.advance_to(clock.now() + 7200)
    with pytest.raises(OpsError, match="outcome unknown"):
        await _probe(conn, _adapter(fake, clock), clock, tmp_path)


async def test_found_pod_ref_is_stored_on_the_row(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock)
    fake.inject("POST", "/pods", "timeout_after")  # created, answer lost
    conn = _db(tmp_path)
    rep = await _probe(conn, _adapter(fake, clock), clock, tmp_path, hold_s=0)
    (row,) = _rows(conn)
    assert rep.ref is not None and row["provider_ref"] == rep.ref and rep.zero
    with pytest.raises(OpsError, match="keeps pod"):
        await _probe(conn, _adapter(fake, clock), clock, tmp_path)


# ------------------------------------------------------------------ old rows (before 0.3.5)
def _old_row(conn: sqlite3.Connection, now: float, *, state: str = "ERROR",
             ref: str | None = None, unknown: int = 0) -> str:  # fmt: skip
    iid = "inst-0123456789ab"
    with tx(conn):
        conn.execute(
            "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
            " created_at, max_lifetime_at, accrued_to, create_called_at, attempts,"
            " last_create_called_at, provider_ref, create_unknown, last_error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,'probe_done')",
            (iid, "0123456789ab", PROBE_POOL, "runpod", "llmhub-0123456789ab-01234567", state,
             now, now, now + 900, now, now, now, ref, unknown),
        )  # fmt: skip
    return iid


async def test_old_error_row_without_ref_is_cleared_after_grace(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock)
    conn = _db(tmp_path)
    iid = _old_row(conn, clock.now())
    with pytest.raises(OpsError, match="grace"):  # create_visibility_grace_s = 600
        await _probe(conn, _adapter(fake, clock), clock, tmp_path)
    assert fake.posts() == 0
    clock.advance_to(clock.now() + 601)
    rep = await _probe(conn, _adapter(fake, clock), clock, tmp_path, hold_s=0)
    old = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    assert old["state"] == "STOPPED" and old["zero_confirmed_at"] is not None
    assert any("old probe row" in n for n in rep.notes) and rep.ref is not None


async def test_old_row_with_listed_label_is_kept(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock)
    conn = _db(tmp_path)
    _old_row(conn, clock.now())
    fake.add_pod("llmhub-0123456789ab-01234567")
    clock.advance_to(clock.now() + 700)
    with pytest.raises(OpsError, match="carry its label"):
        await _probe(conn, _adapter(fake, clock), clock, tmp_path)
    assert fake.posts() == 0


@pytest.mark.parametrize(("state", "ref", "unknown", "match"), [
    ("ERROR", "pod0001abc", 0, "keeps pod"),
    ("ERROR", None, 1, "outcome unknown"),
    ("PROVISIONING", None, 0, "another probe"),
])  # fmt: skip
async def test_old_row_strict_cases(tmp_path: Path, state: str, ref: str | None, unknown: int,
                                    match: str) -> None:  # fmt: skip
    clock = _AutoClock()
    fake = FakeRunpod(clock)
    conn = _db(tmp_path)
    _old_row(conn, clock.now(), state=state, ref=ref, unknown=unknown)
    clock.advance_to(clock.now() + 7200)
    with pytest.raises(OpsError, match=match):
        await _probe(conn, _adapter(fake, clock), clock, tmp_path)
    assert fake.posts() == 0 and len(_rows(conn)) == 1


# ------------------------------------------------------------------ shared REST state
async def test_probe_refused_while_shared_hold(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock)
    conn = _db(tmp_path)
    now = clock.now()
    rate_limit.set_hold(conn, "runpod", now + 600, "http_429", now)
    with pytest.raises(OpsError, match="rate-limited"):
        await _probe(conn, _adapter(fake, clock, limiter=True), clock, tmp_path)
    assert fake.requests == [] and _rows(conn) == []


async def test_probe_429_on_post_is_definite_and_shared(tmp_path: Path) -> None:
    clock = _AutoClock()
    fake = FakeRunpod(clock)
    fake.inject("POST", "/pods", "status", status=429,
                body='{"error":"rate limit exceeded for the hour window","status":429}')
    conn = _db(tmp_path)
    rep = await _probe(conn, _adapter(fake, clock, limiter=True), clock, tmp_path)
    assert rep.attempts == [(G, "13.0", "rate_limited")] and rep.zero and fake.posts() == 1
    assert _rows(conn)[0]["state"] == "STOPPED"
    view = rate_limit.status_view(conn, "runpod", clock.now())
    assert view is not None and view["held"] and view["posts_last_hour"] == 1
    assert view["hold_until"] == pytest.approx(clock.now() + 900, abs=5)
