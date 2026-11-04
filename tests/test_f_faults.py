"""F2/F5: MockProvider faults of plan section 7.3 through the real controller, plus hold and
the "session + 3 jobs" reservation rule on a live GPU."""

from __future__ import annotations

from pathlib import Path

import pytest

from f_fixtures import count, edges
from harness import Harness, make_job
from llmhub.budget_manager.limits import HoldExceedsSessionBudget
from llmhub.provider_adapters.mock import Fault, MockKnobs


def _ts(h: Harness, to_state: str) -> float:
    return float(
        h.conn.execute(
            "SELECT ts FROM transitions WHERE to_state=? ORDER BY id LIMIT 1", (to_state,)
        ).fetchone()[0]
    )


async def test_destroy_partial_volume(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("destroy", Fault.DESTROY_PARTIAL)
    h.submit(make_job(0))
    await h.run_for(400)
    vols = h.provider.store.conn.execute("SELECT * FROM resources WHERE kind='volume'").fetchall()
    assert len(vols) == 1 and vols[0]["destroyed_at"] is not None  # found by label, destroyed
    assert h.state_of() == "STOPPED" and h.provider.live_refs() == []
    assert h.instances()[0]["zero_confirmed_at"] >= vols[0]["destroyed_at"]


async def test_list_stale_no_false_zero(tmp_path: Path) -> None:
    h = Harness(tmp_path, knobs=MockKnobs(list_staleness_s=300))
    h.provider.inject("list_owned", Fault.LIST_STALE, times=3)
    h.submit(make_job(0))
    await h.run_for(1500)
    assert h.state_of() == "STOPPED"
    assert _ts(h, "STOPPED") - _ts(h, "PENDING_DELETE") >= 300  # two zeros >= staleness apart


async def test_destroy_429_no_breaker(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("destroy", Fault.DESTROY_429, times=2)
    h.submit(make_job(0))
    await h.run_for(600)
    assert h.state_of() == "STOPPED"
    stop_at, zero_at = _ts(h, "STOPPING"), _ts(h, "STOPPED")
    assert zero_at - stop_at >= 60  # retry_after 30 s honoured twice
    assert h.conn.execute("SELECT circuit_state FROM provider_accounts").fetchone()[0] == "closed"


async def test_destroy_retry_then_zero(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.provider.inject("destroy", Fault.DESTROY_FAIL, times=8)  # backoff 5..120 s > 180 s
    h.submit(make_job(0))
    await h.run_for(1500)
    trail = h.transitions()
    assert ("STOPPING", "PENDING_DELETE", "stop_confirm_timeout") in trail
    assert h.state_of() == "STOPPED" and h.provider.live_refs() == []
    assert h.instances()[0]["destroy_attempts"] >= 6


async def test_stopped_but_billing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0))
    await h.run_for(120)
    ref = h.instances()[0]["provider_ref"]
    h.provider.stopped_but_billing(ref)
    await h.run_for(200)
    assert ("IDLE", "ERROR", "stopped_but_billing") in h.transitions()
    assert h.state_of() == "STOPPED" and h.provider.live_refs() == []
    inst = h.instances()[0]
    assert inst["accrued_to"] == inst["zero_confirmed_at"]  # billed until the confirmed zero
    billed = h.provider.billed_total()
    assert abs(h.ledger_total() - billed) <= max(0.05, 0.05 * billed)


async def test_get_429_retried_by_guard(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0))
    await h.run_for(100)
    h.provider.inject("get", Fault.RATE_LIMIT_429, times=2)
    await h.run_for(60)
    assert h.state_of() == "IDLE"  # a rate-limited read is retried, never read as "gone"
    assert ("IDLE", "ERROR") not in edges(h)


async def test_hold_and_release(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.submit(make_job(0))
    await h.run_for(100)
    lease = h.controller.hold("p1", 3600)
    await h.run_for(900)
    assert h.state_of() == "BUSY"  # a live hold lease keeps the GPU past idle_timeout
    assert count(h, "SELECT count(*) FROM reservations WHERE scope='hold' AND ref_id=?"
                    " AND released_at IS NULL", lease) == 1  # fmt: skip
    h.controller.release("p1")
    await h.run_for(30)
    assert h.state_of() == "STOPPED"
    assert ("IDLE", "STOPPING", "hold_expired") in h.transitions()
    assert count(h, "SELECT count(*) FROM reservations WHERE scope='hold'"
                    " AND released_at IS NULL") == 0  # fmt: skip
    with pytest.raises(HoldExceedsSessionBudget):
        h.controller.hold("p1", 8 * 3600)  # 0.22 $/h * 8 h > budget_session $1


async def test_session_plus_three_jobs_one_idle_reserve(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    t0 = h.clock.now()
    h.submit(make_job(0, max_tokens=78 * 60))
    await h.run_until(t0 + 80)
    assert h.state_of() == "BUSY"
    for i in range(1, 4):
        h.submit(make_job(i))
    await h.run_until(t0 + 200)
    inst = h.instances()[0]
    r = inst["price_usd_h"] / 3600
    idle_reserved = h.conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM reservations WHERE ref_id LIKE '%:idle'"
    ).fetchone()[0]
    assert idle_reserved <= r * h.cfg.pools["p1"].idle_timeout_s + 1e-9  # one T_idle, not 3
    jobs = count(h, "SELECT count(*) FROM reservations WHERE scope='job' AND ref_id NOT LIKE"
                    " '%:idle'")  # fmt: skip
    assert jobs == 3  # job 0 is paid by the session reservation of its launch
