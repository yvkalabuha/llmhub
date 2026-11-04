"""T-369 G2: migration 0003."""

from __future__ import annotations

from pathlib import Path

from llmhub.persistence.db import MIGRATIONS_DIR, connect, migrate


def _cols(conn, table: str) -> set[str]:  # type: ignore[no-untyped-def]
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn) -> set[str]:  # type: ignore[no-untyped-def]
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_fresh_db(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    assert migrate(conn) >= 3
    assert {"backend", "warnings"} <= _cols(conn, "jobs")
    assert {"jobs_local_queue", "clients_key"} <= _indexes(conn)
    assert migrate(conn) >= 3  # noop


def test_v2_db_migrates_without_loss(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    for f in sorted(MIGRATIONS_DIR.glob("000[12]_*.sql")):
        conn.executescript(f"BEGIN IMMEDIATE;\n{f.read_text()}\nCOMMIT;")
    conn.execute("PRAGMA user_version=2")
    conn.execute("INSERT INTO clients(id,name,key_hash,created_at) VALUES ('c','c','h',0)")
    conn.execute(
        "INSERT INTO jobs(id,client_id,body_sha256,created_at,priority,prio_rank,model_alias,"
        "policy,data_class,status,route,est_tokens_out,req_path)"
        " VALUES ('j','c','s',0,'normal',1,'m','BALANCED','private','queued','cloud',1,'p')"
    )
    assert migrate(conn) >= 3
    row = conn.execute("SELECT id, backend, warnings FROM jobs").fetchone()
    assert tuple(row) == ("j", None, None)
    assert conn.execute("SELECT count(*) FROM clients").fetchone()[0] == 1
