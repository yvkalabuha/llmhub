from __future__ import annotations

from pathlib import Path

import pytest

from llmhub.cost_tracker.ledger import accrue, finalize, session_cost, spent
from test_e_support import HOUR, T0, add_instance, add_ledger, make_conn


def test_accrue_idempotent_and_exact(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn, price=0.36)
    assert accrue(conn, T0 + HOUR) == 1
    assert accrue(conn, T0 + HOUR) == 0  # same now: nothing
    rows = conn.execute("SELECT * FROM ledger").fetchall()
    assert len(rows) == 1 and rows[0]["amount_usd"] == pytest.approx(0.36)
    assert conn.execute("SELECT accrued_to FROM instances").fetchone()[0] == T0 + HOUR
    accrue(conn, T0 + 2 * HOUR)
    assert session_cost(conn, "i1") == pytest.approx(0.72)


def test_accrue_disk_row(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn, price=0.36, offer_json='{"disk_usd_h": 0.036}')
    accrue(conn, T0 + HOUR)
    kinds = {r["kind"]: r["amount_usd"] for r in conn.execute("SELECT * FROM ledger")}
    assert kinds == {"gpu": pytest.approx(0.36), "disk": pytest.approx(0.036)}


@pytest.mark.parametrize(
    "state",
    ["PROVISIONING", "READY", "BUSY", "IDLE", "STOPPING", "PENDING_DELETE", "LEAKED", "ERROR"],
)
def test_accrue_billable_states(tmp_path: Path, state: str) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn, state=state)
    assert accrue(conn, T0 + 60) == 1


def test_stopped_not_billed_and_null_price_skipped(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn, "a", pool="p1", state="STOPPED")
    add_instance(conn, "b", pool="x1", price=None, state="PROVISIONING")
    assert accrue(conn, T0 + 60) == 0
    # accrued_to stays, so the first priced accrual covers the whole interval
    assert conn.execute("SELECT accrued_to FROM instances WHERE id='b'").fetchone()[0] == T0


def test_pending_delete_accrues_until_zero_confirmed(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn, state="PENDING_DELETE")
    accrue(conn, T0 + 600)
    conn.execute("UPDATE instances SET state='STOPPED', zero_confirmed_at=?", (T0 + 900,))
    accrue(conn, T0 + 5000)  # finalizes up to zero_confirmed_at, not beyond
    assert session_cost(conn, "i1") == pytest.approx(0.36 * 900 / HOUR)
    assert accrue(conn, T0 + 9000) == 0


def test_pending_delete_with_zero_confirmed_clamped(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn, state="LEAKED", zero_confirmed_at=T0 + 300)
    accrue(conn, T0 + 1000)
    assert session_cost(conn, "i1") == pytest.approx(0.36 * 300 / HOUR)


def test_finalize(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn)
    finalize(conn, "i1", T0 + 1800)
    assert session_cost(conn, "i1") == pytest.approx(0.18)
    finalize(conn, "i1", T0 + 1800)
    assert session_cost(conn, "i1") == pytest.approx(0.18)


def test_cas_loser_writes_nothing(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_instance(conn)
    other = conn.execute("SELECT * FROM instances").fetchone()
    accrue(conn, T0 + 600)
    # a stale snapshot (accrued_to=T0) must not double-count
    from llmhub.cost_tracker.ledger import _accrue_one

    assert _accrue_one(conn, other, T0 + 600, T0 + 600) is False
    assert session_cost(conn, "i1") == pytest.approx(0.06)


def test_spent_is_max_of_estimate_and_provider_per_provider() -> None:
    import sqlite3

    from llmhub.persistence.db import migrate

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    add_ledger(conn, 1.0, T0, T0 + 100, source="estimate")
    add_ledger(conn, 1.5, T0, T0 + 100, source="provider")
    add_ledger(conn, 2.0, T0, T0 + 100, source="estimate", provider="other")
    add_ledger(conn, 0.5, T0, T0 + 100, source="provider", provider="other")
    assert spent(conn, T0, T0 + 100) == pytest.approx(3.5)
    assert spent(conn, T0, T0 + 100, provider="mock") == pytest.approx(1.5)


def test_spent_prorates_window_edges_and_pools(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    add_ledger(conn, 1.0, T0 - 100, T0 + 100)
    assert spent(conn, T0, T0 + 1000) == pytest.approx(0.5)
    assert spent(conn, T0 - 1000, T0 - 100) == pytest.approx(0.0)
    add_ledger(conn, 1.0, T0 + 10, T0 + 20, pool="x1")
    assert spent(conn, T0, T0 + 1000, pools=["x1"]) == pytest.approx(1.0)
    assert spent(conn, T0, T0 + 1000, pools=[]) == 0.0
