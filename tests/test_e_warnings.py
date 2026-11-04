from __future__ import annotations

from pathlib import Path

from llmhub.budget_manager.limits import warnings, windows
from test_e_support import T0, add_ledger, make_cfg, make_conn


def keys(conn) -> set[str]:  # type: ignore[no-untyped-def]
    return {r["dedup_key"] for r in conn.execute("SELECT dedup_key FROM incidents")}


def test_levels_once_each(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path, budget_day=100.0)
    w = windows(T0, "UTC")
    now = w.month_start + 28 * 86400  # day 29: pace limit is far above
    add_ledger(conn, 22.6, w.month_start, w.month_start + 60)  # 50.2 % of 45
    new = warnings(conn, now, cfg)
    assert new == ["budget:month:50:2026-11"]
    assert warnings(conn, now + 10, cfg) == []  # dedup
    add_ledger(conn, 14.0, w.month_start + 100, w.month_start + 160)  # 81.3 %
    assert warnings(conn, now + 20, cfg) == ["budget:month:80:2026-11"]
    add_ledger(conn, 7.0, w.month_start + 200, w.month_start + 260)  # 96.9 %
    assert warnings(conn, now + 30, cfg) == ["budget:month:95:2026-11"]
    assert warnings(conn, now + 40, cfg) == []
    assert all(
        r["severity"] == "warning"
        for r in conn.execute("SELECT * FROM incidents WHERE kind='budget_warning'")
    )


def test_day_levels_and_new_period(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 1.6, T0 + 1)  # 53 % of the day
    assert "budget:day:50:2026-11-04" in warnings(conn, T0 + 100, cfg)
    add_ledger(conn, 1.6, T0 + 86400 + 1)
    assert "budget:day:50:2026-11-05" in warnings(conn, T0 + 86400 + 100, cfg)


def test_all_levels_at_once_when_far_above(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path)
    add_ledger(conn, 2.9, T0 + 1)
    new = warnings(conn, T0 + 100, cfg)
    assert {
        "budget:day:50:2026-11-04",
        "budget:day:80:2026-11-04",
        "budget:day:95:2026-11-04",
    } <= set(new)


def test_pace_warning(tmp_path: Path) -> None:
    conn, cfg = make_conn(tmp_path), make_cfg(tmp_path, budget_day=100.0)
    w = windows(T0, "UTC")
    # day 4 of 30: allowed 1.25 * 4/30 * 45 = 7.5
    add_ledger(conn, 7.4, w.month_start, w.month_start + 60)
    assert "budget_pace:2026-11" not in warnings(conn, T0, cfg)
    add_ledger(conn, 0.2, w.month_start + 100, w.month_start + 160)
    assert "budget_pace:2026-11" in warnings(conn, T0 + 1, cfg)
    assert "budget_pace:2026-11" not in warnings(conn, T0 + 2, cfg)
