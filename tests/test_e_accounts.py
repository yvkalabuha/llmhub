from __future__ import annotations

from pathlib import Path

from llmhub.budget_manager import accounts
from llmhub.provider_adapters.base import NormalizedError, UnavailableReason
from test_e_support import T0, make_conn


def err(reason: UnavailableReason) -> NormalizedError:
    return NormalizedError(
        reason=reason,
        retryable=False,
        outcome_unknown=False,
        retry_after_s=None,
        http_status=402,
        message="m",
    )


def test_block_and_is_blocked(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert accounts.is_blocked(conn, "mock") is None
    accounts.block_provider(conn, "mock", "insufficient_balance", T0)
    assert accounts.is_blocked(conn, "mock") == "insufficient_balance"
    inc = conn.execute("SELECT * FROM incidents WHERE kind='provider_blocked'").fetchall()
    assert len(inc) == 1 and inc[0]["severity"] == "high"
    accounts.block_provider(conn, "mock", "quota_exceeded", T0 + 1)  # already blocked: no-op
    assert accounts.is_blocked(conn, "mock") == "insufficient_balance"
    assert conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1


def test_balance_growth_does_not_unblock(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    accounts.block_provider(conn, "mock", "insufficient_balance", T0)
    accounts.note_balance(conn, "mock", 500.0, T0 + 60)
    assert accounts.last_balance(conn, "mock") == 500.0
    assert accounts.is_blocked(conn, "mock") == "insufficient_balance"


def test_manual_resume_and_reblock(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    accounts.block_provider(conn, "mock", "insufficient_balance", T0)
    accounts.resume_provider(conn, "mock", T0 + 100)
    assert accounts.is_blocked(conn, "mock") is None
    assert conn.execute("SELECT resolved_at FROM incidents").fetchone()[0] == T0 + 100
    accounts.block_provider(conn, "mock", "insufficient_balance", T0 + 200)  # new incident
    assert conn.execute("SELECT count(*) FROM incidents").fetchone()[0] == 2


def test_handle_provider_error(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert not accounts.handle_provider_error(
        conn, "mock", err(UnavailableReason.PROVIDER_API_ERROR), T0
    )
    assert accounts.is_blocked(conn, "mock") is None
    assert accounts.handle_provider_error(
        conn, "mock", err(UnavailableReason.INSUFFICIENT_BALANCE), T0
    )
    assert accounts.is_blocked(conn, "mock") == "insufficient_balance"
