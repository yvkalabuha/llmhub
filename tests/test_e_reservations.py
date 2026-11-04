from __future__ import annotations

from pathlib import Path

import pytest

from llmhub.budget_manager import reservations as rs
from llmhub.budget_manager.estimate import JobLoad
from llmhub.budget_manager.limits import (
    committed,
    idle_reserved,
    reserve_job_on_running,
    windows,
)
from llmhub.cost_tracker.ledger import accrue
from test_e_support import HOUR, T0, add_instance, make_cfg, make_conn


def _committed(conn, now):  # type: ignore[no-untyped-def]
    w = windows(now, "UTC")
    return committed(conn, w.month_start, w.month_end, now)


def test_no_double_count(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn, price=0.36)
    rs.reserve(conn, "session", "op", 1.0, T0, "i1")
    assert _committed(conn, T0) == pytest.approx(1.0)
    accrue(conn, T0 + 3000)  # 0.30 USD accrued
    assert _committed(conn, T0 + 3000) == pytest.approx(1.0)  # not 1.3


def test_overspend_goes_to_ledger_only(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn, price=0.36)
    rid = rs.reserve(conn, "session", "op", 0.1, T0, "i1")
    accrue(conn, T0 + HOUR)  # 0.36
    row = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    assert rs.remainder(row) == 0 and row["consumed_usd"] == pytest.approx(0.1)
    assert _committed(conn, T0 + HOUR) == pytest.approx(0.36)


def test_fifo_session_then_jobs_by_created(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn)
    s = rs.reserve(conn, "session", "op", 0.1, T0 + 5, "i1")
    j1 = rs.reserve(conn, "job", "j1", 0.1, T0 + 1, "i1")
    j2 = rs.reserve(conn, "job", "j2", 0.1, T0 + 2, "i1")
    left = rs.consume_fifo(conn, "i1", 0.25)
    assert left == 0
    c = {r["id"]: r["consumed_usd"] for r in conn.execute("SELECT * FROM reservations")}
    assert (
        c[s] == pytest.approx(0.1) and c[j1] == pytest.approx(0.1) and c[j2] == pytest.approx(0.05)
    )
    assert rs.consume_fifo(conn, "i1", 1.0) == pytest.approx(0.95)


def test_released_not_counted(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn)
    rid = rs.reserve(conn, "session", "op", 1.0, T0, "i1")
    rs.release(conn, rid, T0 + 1, "E20")
    assert rs.open_remainder_total(conn) == 0
    rs.reserve(conn, "job", "j", 0.3, T0, "i1")
    rs.reserve(conn, "job", "j:idle", 0.1, T0, "i1")
    assert rs.release_ref(conn, "job", "j", T0, "done") == 2
    assert rs.release_instance(conn, "i1", T0, "x") == 0


def test_session_plus_three_jobs_one_idle_reserve(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    cfg = make_cfg(tmp_path)
    add_instance(conn, price=0.36)
    r = 0.36 / 3600
    t_idle = 300
    rs.reserve(conn, "session", "op", r * (540 + 300) + 0.001, T0, "i1")  # cold + idle
    inst = conn.execute("SELECT * FROM instances").fetchone()
    assert idle_reserved(conn, inst, cfg) == pytest.approx(r * t_idle)
    for k in range(3):
        g = reserve_job_on_running(conn, "i1", JobLoad(f"j{k}", 1200, 780), T0, cfg)
        assert g.ok, g
    idle_rows = conn.execute(
        "SELECT amount_usd FROM reservations WHERE ref_id LIKE '%:idle'"
    ).fetchall()
    assert idle_rows == []  # session already holds one full T_idle, nothing to top up
    inst = conn.execute("SELECT * FROM instances").fetchone()
    assert idle_reserved(conn, inst, cfg) == pytest.approx(r * t_idle)  # one T_idle, not 3


def test_idle_topped_up_when_session_remainder_small(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    cfg = make_cfg(tmp_path)
    add_instance(conn, price=0.36)
    r = 0.36 / 3600
    rs.reserve(conn, "session", "op", r * 100, T0, "i1")  # only 100 s of idle left
    for k in range(3):
        assert reserve_job_on_running(conn, "i1", JobLoad(f"j{k}", 1200, 780), T0, cfg).ok
    total_idle = sum(
        rs.remainder(x)
        for x in conn.execute("SELECT * FROM reservations")
        if x["scope"] == "session" or x["ref_id"].endswith(":idle")
    )
    assert total_idle == pytest.approx(r * 300)  # topped up to exactly one T_idle
