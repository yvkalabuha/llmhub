"""T-372 D1/D2: ledger rows are excluded (never deleted); every ledger sum skips them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from llmhub.configuration.models import Config
from llmhub.cost_tracker.ledger import session_cost, spent
from llmhub.cost_tracker.reconcile import _ours_estimate
from llmhub.persistence.db import connect, migrate
from llmhub.tools.t372_ops import OpsError, ledger_exclude

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_793_750_400.0


def _db(tmp: Path):  # type: ignore[no-untyped-def]
    conn = connect(tmp / "state.db")
    migrate(conn)
    for i, (prov, amount) in enumerate([("mock", 1.0), ("mock", 2.0), ("runpod", 0.5)]):
        iid = f"i{i}"
        conn.execute(
            "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
            " created_at, max_lifetime_at, accrued_to) VALUES (?,?,?,?,?,'STOPPED',?,?,?,?)",
            (iid, f"{i:012x}", f"p{i}", prov, f"llmhub-{i:012x}-00000000", T0, T0, T0 + 1, T0),
        )  # fmt: skip
        conn.execute(
            "INSERT INTO ledger(instance_id, provider, pool, kind, from_ts, to_ts, amount_usd,"
            " source, created_at) VALUES (?,?,?,'gpu',?,?,?,'estimate',?)",
            (iid, prov, f"p{i}", T0, T0 + 3600, amount, T0),
        )  # fmt: skip
    return conn


def _cfg(tmp: Path, enabled: list[str]) -> Config:
    cfg = Config(data_dir=tmp, pools={})
    cfg.providers.enabled = enabled
    return cfg


def test_migration_adds_columns(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ledger)")}
    assert {"excluded_at", "excluded_reason"} <= cols
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 4


def test_sums_skip_excluded_rows(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert spent(conn, T0, T0 + 3600) == pytest.approx(3.5)
    conn.execute("UPDATE ledger SET excluded_at=1, excluded_reason='x' WHERE provider='mock'")
    assert spent(conn, T0, T0 + 3600) == pytest.approx(0.5)
    assert spent(conn, T0, T0 + 3600, provider="mock") == 0
    assert session_cost(conn, "i0") == 0 and session_cost(conn, "i2") == 0.5
    assert _ours_estimate(conn, "mock", T0, T0 + 3600) == 0
    assert _ours_estimate(conn, "runpod", T0, T0 + 3600) == pytest.approx(0.5)


def test_every_ledger_read_in_src_filters_excluded() -> None:
    """Grep step of D1: each `FROM ledger` statement in src mentions excluded_at."""
    for path in (ROOT / "src" / "llmhub").rglob("*.py"):
        text = path.read_text()
        for m in re.finditer(r"FROM ledger", text):
            window = text[max(0, m.start() - 600): m.start() + 400]  # spent() builds WHERE first
            assert "excluded_at" in window, f"{path.name}: {text[m.start():m.start() + 120]!r}"


def test_exclude_refused_while_provider_enabled(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    with pytest.raises(OpsError, match="providers.enabled"):
        ledger_exclude(conn, _cfg(tmp_path, ["mock"]), "mock", reason="t", yes=True, now=T0,
                       backup_dir=None)  # fmt: skip


def test_exclude_refused_with_live_mock_instance(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("UPDATE instances SET state='READY' WHERE id='i1'")
    with pytest.raises(OpsError, match="not STOPPED"):
        ledger_exclude(conn, _cfg(tmp_path, ["runpod"]), "mock", reason="t", yes=True, now=T0,
                       backup_dir=None)  # fmt: skip


def test_dry_run_then_apply_with_backup(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    cfg = _cfg(tmp_path, ["runpod"])
    dry = ledger_exclude(conn, cfg, "mock", reason="t372-mock", yes=False, now=T0,
                         backup_dir=tmp_path / "bk")  # fmt: skip
    assert (dry.rows, dry.usd, dry.dry_run, dry.backup) == (2, 3.0, True, None)
    assert spent(conn, T0, T0 + 3600) == pytest.approx(3.5)
    assert not (tmp_path / "bk").exists()
    res = ledger_exclude(conn, cfg, "mock", reason="t372-mock", yes=True, now=T0 + 5,
                         backup_dir=tmp_path / "bk")  # fmt: skip
    assert res.rows == 2 and res.backup is not None and Path(res.backup).is_file()
    assert conn.execute("SELECT count(*) FROM ledger").fetchone()[0] == 3  # nothing deleted
    rows = conn.execute("SELECT excluded_at, excluded_reason FROM ledger WHERE provider='mock'"
                        ).fetchall()  # fmt: skip
    assert {(r[0], r[1]) for r in rows} == {(T0 + 5, "t372-mock")}
    inc = conn.execute("SELECT severity FROM incidents WHERE kind='ledger_excluded'").fetchall()
    assert [r[0] for r in inc] == ["info"]
    again = ledger_exclude(conn, cfg, "mock", reason="t372-mock", yes=True, now=T0 + 9,
                           backup_dir=None)  # fmt: skip
    assert again.rows == 0
    backup = connect(res.backup)
    assert backup.execute("SELECT count(*) FROM ledger WHERE excluded_at IS NULL"
                          ).fetchone()[0] == 3  # fmt: skip
