from __future__ import annotations

from pathlib import Path

import pytest

from llmhub.budget_manager.estimate import (
    JobLoad,
    estimate_launch,
    get_speeds,
    t_cold_p90,
    update_speed_ewma,
    work_seconds,
)
from test_e_support import add_instance, make_conn


def test_e_launch_matches_plan_example() -> None:
    # plan E-1: 540 + 5 + 102.6 + 300 s (work 107.6 s for 6000 in / 8000 out tokens)
    est = estimate_launch(0.22, [JobLoad("j", 6000, 8000)], 540, 300, 1200, 78)
    assert est.usd == pytest.approx(0.22 / 3600 * (540 + 5 + 8000 / 78 + 300))
    assert est.usd == pytest.approx(0.0579, abs=1e-4)


def test_per_job_share_and_traffic() -> None:
    jobs = [JobLoad("a", 1200, 780), JobLoad("b", 1200, 780)]
    est = estimate_launch(0.36, jobs, 540, 300, 1200, 78, traffic_usd=0.01, storage_usd=0.02)
    r = 0.36 / 3600
    assert est.usd == pytest.approx(r * (540 + 2 * work_seconds(1200, 780, 1200, 78) + 300) + 0.03)
    assert est.per_job["a"] == pytest.approx(r * work_seconds(1200, 780, 1200, 78) + r * 840 / 2)


def test_t_cold_p90_nearest_rank(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert t_cold_p90(conn, "p1", 540) == 540  # no starts
    for i, d in enumerate([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]):
        add_instance(
            conn,
            f"i{i}",
            state="STOPPED",
            created=1000.0 + i * 10000,
            ready_at=1000.0 + i * 10000 + d,
        )
    assert t_cold_p90(conn, "p1", 540) == 900  # ceil(0.9*10)=9th value
    assert t_cold_p90(conn, "x1", 540) == 540


def test_t_cold_default_below_three(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    for i in range(2):
        add_instance(
            conn, f"i{i}", state="STOPPED", created=1000.0 * (i + 1), ready_at=1000.0 * (i + 1) + 50
        )
    assert t_cold_p90(conn, "p1", 540) == 540


def test_ewma(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert get_speeds(conn, "p1", 1200, 78) == (1200, 78)
    update_speed_ewma(conn, "p1", None, 100.0, pp0=1200, tg0=78)
    pp, tg = get_speeds(conn, "p1", 1200, 78)
    assert pp == 1200
    assert tg == pytest.approx(0.3 * 100 + 0.7 * 78)
    update_speed_ewma(conn, "p1", None, 100.0)
    assert get_speeds(conn, "p1", 1200, 78)[1] == pytest.approx(0.3 * 100 + 0.7 * tg)
