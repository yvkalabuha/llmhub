"""D2: auth_request validator on a real (T-368 schema) SQLite DB."""

import logging
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from llmhub.clock import FakeClock
from llmhub.persistence.db import connect, migrate
from llmhub.weights import auth_app
from llmhub.weights.acct import AcctState
from llmhub.weights.auth_app import build_auth_app, main, requested_bytes
from llmhub.weights.tokens import issue_token

SECRET = b"w" * 32
OP = "0123456789ab"
OP2 = "ba9876543210"
REL = "qwen/model.gguf"
SIZE = 1000
TOTAL = 1100  # model + draft; cap = 1.2 * 1100 = 1320


def _insert(conn: sqlite3.Connection, op: str, state: str) -> None:
    conn.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since, created_at,"
        " max_lifetime_at) VALUES (?, ?, ?, 'mock', ?, ?, 0, 0, 1e12)",
        (f"inst-{op}", op, f"pool{op}", f"label-{op}", state),
    )


@pytest.fixture
def env(tmp_path: Path):
    db = tmp_path / "state.db"
    conn = connect(db)
    migrate(conn)
    _insert(conn, OP, "LOADING_MODEL")
    log = tmp_path / "weights-acct.log"
    log.write_text("")
    clock = FakeClock(1_000.0)
    acct = AcctState(tmp_path / "auth", log)
    app = build_auth_app(db, SECRET, frozenset({REL}), clock, acct=acct, sizes={REL: (SIZE, TOTAL)})
    client = TestClient(app, client=("127.0.0.1", 5000))
    yield type(
        "E",
        (),
        {
            "conn": conn,
            "client": client,
            "log": log,
            "db": db,
            "clock": clock,
            "app": app,
            "tmp": tmp_path,
        },
    )
    conn.close()


def _get(
    env,
    op: str = OP,
    token: str | None = "ok",
    ip: str = "198.51.100.7",
    rng: str | None = None,
    rel: str = REL,
    exp: int = 5000,
):
    h = {"X-Original-URI": f"/w/{op}/{rel}", "X-Real-IP": ip}
    if token == "ok":
        h["Authorization"] = "Bearer " + issue_token(SECRET, op, exp)
    elif token:
        h["Authorization"] = token
    if rng:
        h["X-Range"] = rng
    return env.client.get("/weights/auth", headers=h)


def test_204_for_valid_token(env) -> None:
    assert _get(env).status_code == 204


def test_401_missing_and_403_cases(env) -> None:
    assert _get(env, token=None).status_code == 401
    assert _get(env, token="Bearer junk").status_code == 403
    assert _get(env, rel="other/x.gguf").status_code == 403
    assert _get(env, op=OP2).status_code == 403  # token for OP2 but unknown op -> 403
    env.clock.advance_to(5000)
    assert _get(env).status_code == 403  # expired


def test_revoked_after_ready(env) -> None:
    assert _get(env).status_code == 204
    env.conn.execute("UPDATE instances SET state='READY' WHERE op_id=?", (OP,))
    assert _get(env).status_code == 403
    env.conn.execute("UPDATE instances SET state='LOADING_MODEL' WHERE op_id=?", (OP,))
    assert _get(env).status_code == 204  # state is the only source of truth


def test_db_is_read_only(env) -> None:
    conn = auth_app._open_ro(env.db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE instances SET state='READY'")
    conn.close()


def test_db_busy_gives_503(env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_p: Path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(auth_app, "_open_ro", boom)
    assert _get(env).status_code == 503


def test_missing_db_gives_503(env) -> None:
    env.db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(env.db) + suffix).unlink(missing_ok=True)
    assert _get(env).status_code == 503


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_d2b_read_only_without_dir_write(env) -> None:
    """Writer holds the WAL DB open; validator reads with dir 0550 and -wal/-shm 0440."""
    env.conn.execute("UPDATE instances SET state='LOADING_MODEL'")
    wal, shm = Path(str(env.db) + "-wal"), Path(str(env.db) + "-shm")
    assert wal.exists() and shm.exists()
    assert sqlite3.sqlite_version_info >= (3, 22)
    d = env.db.parent
    for p in (wal, shm):
        p.chmod(0o440)
    d.chmod(0o550)
    try:
        # a fresh process-equivalent: new connection, no write access anywhere
        assert _get(env).status_code == 204
        conn = auth_app._open_ro(env.db)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM instances")
        conn.close()
    finally:
        d.chmod(0o750 | stat.S_IWUSR)
        for p in (wal, shm):
            p.chmod(0o640)


def test_caplog_has_op_and_verdict_not_token(env, caplog: pytest.LogCaptureFixture) -> None:
    token = issue_token(SECRET, OP, 5000)
    with caplog.at_level(logging.INFO, logger="llmhub.weights.auth"):
        _get(env)
        _get(env, token="Bearer " + token[:-3] + "AAA")
    text = caplog.text
    assert f"op={OP} verdict=ok" in text
    assert "bad_signature" in text
    assert token not in text and token[:20] not in text and "198.51.100.7" not in text


def test_ip_binding_first_ip_wins(env) -> None:
    assert _get(env, ip="198.51.100.7").status_code == 204
    assert _get(env, ip="198.51.100.7").status_code == 204
    assert _get(env, ip="203.0.113.9").status_code == 403
    assert _get(env, ip="").status_code == 403


def test_ip_binding_persists_in_state_db(env) -> None:
    _get(env, ip="198.51.100.7")
    acct2 = AcctState(env.tmp / "auth", env.log)
    assert acct2.bind_ip(OP, "198.51.100.7") and not acct2.bind_ip(OP, "203.0.113.9")


@pytest.mark.parametrize(
    "hdr,size,want",
    [
        (None, 1000, 1000),
        ("bytes=0-99", 1000, 100),
        ("bytes=900-", 1000, 100),
        ("bytes=900-5000", 1000, 100),
        ("bytes=-50", 1000, 50),
        ("bytes=5000-6000", 1000, 0),
        ("bytes=0-1,5-9", 1000, 1000),
        ("garbage", 1000, 1000),
        ("bytes=-", 1000, 1000),
    ],
)
def test_requested_bytes(hdr: str | None, size: int, want: int) -> None:
    assert requested_bytes(hdr, size) == want


def test_volume_cap_and_resume(env) -> None:
    # honest case: dropped at 50 %, resume of the remainder
    assert _get(env, rng="bytes=0-").status_code == 204  # 1000 <= 1320
    env.log.write_text(f"{OP} 500 206\n")
    assert _get(env, rng="bytes=500-").status_code == 204  # 500 + 500 <= 1320
    env.log.write_text(f"{OP} 500 206\n{OP} 500 206\n")
    assert _get(env, rng="bytes=0-299").status_code == 204  # 1000 + 300 <= 1320
    assert _get(env, rng="bytes=0-399").status_code == 403  # 1000 + 400 > 1320
    assert _get(env).status_code == 403  # whole file again


def test_acct_log_incremental_partial_line_and_rotation(tmp_path: Path) -> None:
    log = tmp_path / "acct.log"
    log.write_text(f"{OP} 100 200\n{OP2} 5 200\nnot a line\n{OP} 20")
    a = AcctState(tmp_path / "st", log)
    assert (a.served(OP), a.served(OP2)) == (100, 5)  # partial last line ignored
    with log.open("a") as f:
        f.write(f" 200\n{OP} 1 200\n")
    assert a.served(OP) == 121  # only the new tail was added, no double counting
    assert a.served(OP) == 121
    # rotation: new inode -> read from the beginning of the new file; old sums are kept
    log.rename(tmp_path / "acct.log.1")
    log.write_text(f"{OP} 90 200\n")
    assert a.served(OP) == 211
    # restart with the same state dir resumes without double counting
    b = AcctState(tmp_path / "st", log)
    assert b.served(OP) == 211
    # truncation (file shorter than the stored offset): restart from the beginning
    log.write_text(f"{OP} 7 200\n")  # 19 bytes < stored offset 20
    assert b.served(OP) == 218


def test_acct_log_missing_is_zero(tmp_path: Path) -> None:
    assert AcctState(tmp_path / "st", tmp_path / "none.log").served(OP) == 0


def test_state_dir_private(tmp_path: Path) -> None:
    AcctState(tmp_path / "st", tmp_path / "l")
    assert stat.S_IMODE((tmp_path / "st").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "st" / "auth-state.db").stat().st_mode) == 0o600


def test_metrics_counters_local_only(env) -> None:
    _get(env)
    _get(env, token=None)
    _get(env, token="Bearer junk")
    r = env.client.get("/metrics")
    assert r.status_code == 200
    assert 'verdict="ok"} 1' in r.text and 'verdict="missing"} 1' in r.text
    assert 'verdict="malformed"} 1' in r.text
    remote = TestClient(env.app, client=("203.0.113.5", 1))
    assert remote.get("/metrics").status_code == 403


def test_main_requires_secret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"data_dir: {tmp_path}\n")
    assert main(["--config", str(cfg)], env={}, run=lambda *a, **k: None) == 2
    assert "LLMHUB_WEIGHTS_SECRET" in capsys.readouterr().err


def test_main_listens_on_configured_address(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        f"data_dir: {tmp_path}\n"
        "weights:\n"
        "  auth_listen: 127.0.0.1:8123\n"
        f"  auth_state_dir: {tmp_path}/st\n"
        f"  acct_log: {tmp_path}/acct.log\n"
    )
    seen: dict[str, object] = {}

    def fake_run(app: object, **kw: object) -> None:
        seen.update(kw, app=app)

    rc = main(["--config", str(cfg)], env={"LLMHUB_WEIGHTS_SECRET": "z" * 40}, run=fake_run)
    assert rc == 0
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 8123)
