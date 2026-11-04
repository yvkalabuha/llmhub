from __future__ import annotations

from pathlib import Path

import pytest

from llmhub.budget_manager import accounts
from llmhub.budget_manager import reservations as rs
from llmhub.budget_manager.estimate import JobLoad, LaunchEstimate, estimate_launch
from llmhub.budget_manager.limits import (
    HoldExceedsSessionBudget,
    Stage,
    check_hold,
    check_job_on_running,
    check_launch,
    close_hold_prewarm_if_exhausted,
    launch_gate,
    stage,
    windows,
)
from llmhub.lifecycle_manager.machine import insert_provisioning
from llmhub.provider_adapters.base import UnavailableReason
from llmhub.task_queue.models import Priority
from test_e_support import HOUR, T0, add_instance, add_ledger, make_cfg, make_conn

N = Priority.NORMAL


def est(usd: float, per_job: float = 0.01, price: float = 0.22) -> LaunchEstimate:
    return LaunchEstimate(
        usd=usd, t_cold_s=540, work_s=10, idle_s=300, per_job={"j": per_job}, price_usd_h=price
    )


def test_launch_ok(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    assert check_launch(conn, est(0.06), "mock", T0, cfg, N, None).ok


def test_halted(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    conn.execute("INSERT INTO controller_state VALUES ('halted','1',0)")
    g = check_launch(conn, est(0.06), "mock", T0, cfg, N, None)
    assert not g.ok and g.detail == "controller_halted"


def test_session_and_job_limits(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    g = check_launch(conn, est(1.01), "mock", T0, cfg, N, None)
    assert g.reason == UnavailableReason.BUDGET_LIMIT_REACHED and g.detail == "session"
    assert check_launch(conn, est(1.0), "mock", T0, cfg, N, None).ok
    g = check_launch(conn, est(0.5, per_job=0.26), "mock", T0, cfg, N, None)
    assert g.detail == "job"
    assert check_launch(conn, est(0.5, per_job=0.25), "mock", T0, cfg, N, None).ok


def test_day_limit(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 2.5, T0 + 10)
    g = check_launch(conn, est(0.6), "mock", T0 + 100, cfg, Priority.URGENT, None)
    assert g.detail == "day"
    assert check_launch(conn, est(0.5), "mock", T0 + 100, cfg, Priority.URGENT, None).ok


def test_month_limit(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    w = windows(T0, "UTC")
    add_ledger(conn, 44.6, w.month_start, w.month_start + 60)  # earlier in the month
    g = check_launch(conn, est(0.5), "mock", T0, cfg, Priority.URGENT, None)
    assert g.detail == "month"
    assert check_launch(conn, est(0.4), "mock", T0, cfg, Priority.URGENT, None).ok


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.79, Stage.NORMAL),
        (0.80, Stage.ECONOMY),
        (0.95, Stage.URGENT_ONLY),
        (1.0, Stage.NO_LAUNCH),
    ],
)
def test_stages_month(tmp_path: Path, ratio: float, expected: Stage) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path, budget_day=100.0)
    w = windows(T0, "UTC")
    add_ledger(conn, 45.0 * ratio, w.month_start + 10)
    assert stage(conn, T0, cfg) == expected


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.79, Stage.NORMAL),
        (0.80, Stage.ECONOMY),
        (0.95, Stage.URGENT_ONLY),
        (1.0, Stage.NO_LAUNCH),
    ],
)
def test_stages_day(tmp_path: Path, ratio: float, expected: Stage) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 3.0 * ratio, T0 + 10)
    assert stage(conn, T0 + 100, cfg) == expected


def test_stage_gates(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 2.9, T0 + 10)  # 96.7 % of the day
    g = check_launch(conn, est(0.05), "mock", T0 + 100, cfg, N, None)
    assert g.detail == "stage:urgent_only"
    assert check_launch(conn, est(0.05), "mock", T0 + 100, cfg, Priority.URGENT, None).ok
    add_ledger(conn, 0.1, T0 + 20)
    g = check_launch(conn, est(0.01), "mock", T0 + 100, cfg, Priority.URGENT, None)
    assert g.detail == "stage:no_launch"


def test_reservations_count_toward_stage(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_instance(conn)
    rs.reserve(conn, "session", "op", 2.5, T0, "i1")
    assert stage(conn, T0, cfg) == Stage.ECONOMY


def test_subbudget_exception(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 9.5, T0 - 3 * 86400, T0 - 3 * 86400 + 60, pool="x1")
    g = check_launch(conn, est(0.6), "mock", T0, cfg, N, "exception")
    assert g.detail == "subbudget:exception"
    assert check_launch(conn, est(0.5), "mock", T0, cfg, N, "exception").ok
    # other pools do not count for the sub-budget
    assert check_launch(conn, est(0.6), "mock", T0, cfg, N, None).ok


def test_balance_blocks_provider_once(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    accounts.note_balance(conn, "mock", 1.2, T0)
    g = check_launch(conn, est(0.5), "mock", T0, cfg, N, None)  # 0.5 + 1.0 reserve > 1.2
    assert g.reason == UnavailableReason.INSUFFICIENT_BALANCE
    assert accounts.is_blocked(conn, "mock") == "insufficient_balance"
    accounts.note_balance(conn, "mock", 50.0, T0 + 5)  # money arrived: still blocked
    g = check_launch(conn, est(0.5), "mock", T0 + 5, cfg, N, None)
    assert g.reason == UnavailableReason.INSUFFICIENT_BALANCE and "blocked" in g.detail
    accounts.resume_provider(conn, "mock", T0 + 10)
    assert check_launch(conn, est(0.5), "mock", T0 + 10, cfg, N, None).ok


def test_min_balance_hours(tmp_path: Path) -> None:
    conn, cfg = (
        make_conn(
            tmp_path,
        ),
        make_cfg(tmp_path, reserve_usd=0.0),
    )
    accounts.note_balance(conn, "mock", 0.2, T0)  # < 0.22 USD/h * 1 h
    assert not check_launch(conn, est(0.05), "mock", T0, cfg, N, None).ok


def test_windows_timezone_and_utc_default(tmp_path: Path) -> None:
    w = windows(T0 + 3600, "UTC")
    assert w.day_start == T0 and w.day_end == T0 + 86400
    assert w.month_key == "2026-11" and w.days_in_month == 30 and w.day_of_month == 4
    w2 = windows(T0 + 3600, "Europe/Moscow")  # 04:00 local, day started 21:00Z before
    assert w2.day_start == T0 - 3 * 3600
    assert w2.month_start == 1_793_750_400.0 - 3 * 86400 - 3 * 3600
    w3 = windows(1_793_750_400.0 - 3600, "UTC")  # 23:00Z 2026-11-03
    assert w3.day_key == "2026-11-03" and w3.month_key == "2026-11"
    oct_w = windows(T0 - 5 * 86400, "UTC")
    assert oct_w.month_key == "2026-10" and oct_w.month_end == oct_w.month_start + 31 * 86400


def test_timezone_changes_day_boundary(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    cfg_msk = make_cfg(tmp_path, timezone="Europe/Moscow")
    # 22:00Z previous day is already 01:00 on the 4th in Moscow
    add_ledger(conn, 2.9, T0 - 2 * 3600, T0 - 2 * 3600 + 1)
    assert stage(conn, T0 + 3600, cfg_msk) == Stage.URGENT_ONLY
    assert stage(conn, T0 + 3600, make_cfg(tmp_path)) == Stage.NORMAL


def test_hold_exceeds_session(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    with pytest.raises(HoldExceedsSessionBudget) as ei:
        check_hold(conn, 0.36, 3 * HOUR, T0, cfg)  # 1.08 > 1.0
    assert ei.value.reason == UnavailableReason.BUDGET_LIMIT_REACHED
    assert check_hold(conn, 0.36, 2 * HOUR, T0, cfg).ok
    cfg2 = make_cfg(tmp_path)
    cfg2 = cfg2.model_copy(
        update={"leases": cfg2.leases.model_copy(update={"hold_may_exceed_session": True})}
    )
    assert check_hold(conn, 0.36, 3 * HOUR, T0, cfg2).ok


def test_hold_day_limit(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 2.5, T0 + 1)
    assert not check_hold(conn, 0.36, 2 * HOUR, T0 + 5, cfg).ok


def test_close_hold_prewarm_at_100(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_instance(conn)
    rs.reserve(conn, "hold", "h", 0.5, T0, "i1")
    rs.reserve(conn, "prewarm", "p", 0.5, T0, "i1")
    assert close_hold_prewarm_if_exhausted(conn, T0, cfg) == 0
    add_ledger(conn, 3.0, T0 + 1)
    assert close_hold_prewarm_if_exhausted(conn, T0 + 10, cfg) == 2


def test_job_on_running_checks(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_instance(conn, price=0.36)
    assert check_job_on_running(conn, "i1", JobLoad("j", 1200, 780), T0, cfg).ok
    # budget_job: huge generation
    g = check_job_on_running(conn, "i1", JobLoad("j", 1200, 10_000_000), T0, cfg)
    assert g.detail == "job"
    # session: spend 0.95 already
    add_ledger(conn, 0.98, T0, T0 + 10, instance_id="i1")
    g = check_job_on_running(conn, "i1", JobLoad("j", 1200, 780), T0 + 10, cfg)
    assert g.detail == "session"


def test_launch_gate_inside_insert_provisioning(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    e = estimate_launch(0.22, [JobLoad("j", 1200, 780)], 540, 300, 1200, 78)
    gate = launch_gate(e, "mock", T0, cfg, N, None, "op1")
    iid = insert_provisioning(
        conn,
        pool="p1",
        provider="mock",
        op_id="op1",
        label="llmhub-op1-aaaaaaaa",
        max_lifetime_at=T0 + 100,
        now=T0,
        gate=gate,
    )
    assert iid == "inst-op1"
    res = conn.execute("SELECT * FROM reservations").fetchall()
    assert len(res) == 1 and res[0]["scope"] == "session" and res[0]["instance_id"] == iid
    assert res[0]["amount_usd"] == pytest.approx(e.usd)
    # second launch in the same pool: UNIQUE fails -> reservation rolled back
    gate2 = launch_gate(e, "mock", T0, cfg, N, None, "op2")
    assert (
        insert_provisioning(
            conn,
            pool="p1",
            provider="mock",
            op_id="op2",
            label="llmhub-op2-aaaaaaaa",
            max_lifetime_at=T0 + 100,
            now=T0,
            gate=gate2,
        )
        is None
    )
    assert conn.execute("SELECT count(*) FROM reservations").fetchone()[0] == 1


def test_launch_gate_refuses_when_blocked(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    accounts.block_provider(conn, "mock", "quota_exceeded", T0)
    e = est(0.05)
    gate = launch_gate(e, "mock", T0, cfg, N, None, "op1")
    assert (
        insert_provisioning(
            conn,
            pool="p1",
            provider="mock",
            op_id="op1",
            label="llmhub-op1-aaaaaaaa",
            max_lifetime_at=T0 + 100,
            now=T0,
            gate=gate,
        )
        is None
    )
    assert conn.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
