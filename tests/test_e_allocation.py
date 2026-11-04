from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from llmhub.budget_manager.allocation import UNATTRIBUTED, allocate_session
from test_e_support import HOUR, T0, add_instance, add_ledger, make_conn


def add_job(conn: Any, jid: str, client: str, cost: float, start: float, end: float) -> None:
    conn.execute(
        "INSERT INTO jobs(id, client_id, body_sha256, created_at, priority, prio_rank, model_alias,"
        " policy, data_class, status, route, est_tokens_out, req_path, instance_id, started_at,"
        " finished_at, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, client, "h", T0, "normal", 1, "m1", "BALANCED", "private", "completed", "cloud", 10,
         "p", "i1", start, end, cost),
    )  # fmt: skip


def add_lease(conn: Any, lid: str, kind: str, client: str | None, a: float, b: float) -> None:
    conn.execute(
        "INSERT INTO leases(id, holder, client_id, kind, pool, ttl_s, expires_at, created_at, closed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (lid, "h", client, kind, "p1", b - a, b, a, b),
    )


def setup(tmp_path: Path) -> Any:
    conn = make_conn(tmp_path)
    add_instance(conn, price=0.36, accrued_to=T0 + HOUR)
    add_ledger(conn, 0.36, T0, T0 + HOUR, instance_id="i1")
    return conn


def test_shared_split_proportional_to_work(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    add_job(conn, "a", "c1", 0.02, T0 + 600, T0 + 700)
    add_job(conn, "b", "c2", 0.06, T0 + 800, T0 + 1000)
    res = allocate_session(conn, "i1")
    shared = 0.36 - 0.08
    assert res["c1"] == (pytest.approx(0.02), pytest.approx(shared * 0.25))
    assert res["c2"] == (pytest.approx(0.06), pytest.approx(shared * 0.75))
    assert sum(w + s for w, s in res.values()) == pytest.approx(0.36)
    rows = conn.execute("SELECT * FROM cost_allocations").fetchall()
    assert len(rows) == 2


def test_hold_without_work_goes_to_holder(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    add_lease(conn, "l1", "hold", "c1", T0, T0 + HOUR)
    res = allocate_session(conn, "i1")
    assert res == {"c1": (0.0, pytest.approx(0.36))}


def test_hold_plus_work(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    add_job(conn, "a", "c2", 0.04, T0 + 1800, T0 + 2400)  # 600 s busy
    add_lease(conn, "l1", "hold", "c1", T0, T0 + 1800)  # 1800 s idle under hold = 0.18
    res = allocate_session(conn, "i1")
    assert res["c1"][0] == 0.0 and res["c1"][1] == pytest.approx(0.18)
    assert res["c2"][0] == pytest.approx(0.04)
    assert res["c2"][1] == pytest.approx(0.36 - 0.18 - 0.04)


def test_hold_excludes_busy_periods(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    add_job(conn, "a", "c2", 0.05, T0 + 600, T0 + 1200)
    add_lease(conn, "l1", "prewarm", None, T0, T0 + 1800)  # admin; 1200 s without a job
    res = allocate_session(conn, "i1")
    assert res["admin"][1] == pytest.approx(0.36 / HOUR * 1200)


def test_no_work_no_holder_unattributed(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    assert allocate_session(conn, "i1") == {UNATTRIBUTED: (0.0, pytest.approx(0.36))}


def test_never_allocates_more_than_spent_and_is_rewritable(tmp_path: Path) -> None:
    conn = setup(tmp_path)
    add_lease(conn, "l1", "hold", "c1", T0, T0 + HOUR)
    add_lease(conn, "l2", "hold", "c2", T0, T0 + HOUR)  # overlapping holds
    res = allocate_session(conn, "i1")
    assert sum(w + s for w, s in res.values()) == pytest.approx(0.36)
    allocate_session(conn, "i1")
    assert conn.execute("SELECT count(*) FROM cost_allocations").fetchone()[0] == 2


def test_unknown_instance(tmp_path: Path) -> None:
    assert allocate_session(make_conn(tmp_path), "nope") == {}
