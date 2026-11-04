"""T-370 fix round 1: weights server findings SEC-1/2/4/8 (unit level, no docker)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from llmhub.clock import FakeClock
from llmhub.persistence.db import connect, migrate
from llmhub.weights import acct as acct_mod
from llmhub.weights.acct import AcctState
from llmhub.weights.auth_app import build_auth_app
from llmhub.weights.tokens import issue_token

ROOT = Path(__file__).resolve().parent.parent
SECRET = b"w" * 32
OP = "0123456789ab"
REL = "qwen/model.gguf"
SIZE = 1000
TOTAL = 1100  # cap 1320


def _code() -> str:
    text = (ROOT / "deploy" / "nginx-weights.conf").read_text(encoding="utf-8")
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _app(tmp_path: Path, acct: AcctState | None = None) -> tuple[TestClient, Path]:
    db = tmp_path / "state.db"
    conn = connect(db)
    migrate(conn)
    conn.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since, created_at,"
        " max_lifetime_at) VALUES ('i1', ?, 'p', 'mock', 'l', 'LOADING_MODEL', 0, 0, 1e12)",
        (OP,),
    )
    conn.close()
    log = tmp_path / "weights-acct.log"
    log.write_text("")
    a = acct or AcctState(tmp_path / "auth", log)
    app = build_auth_app(db, SECRET, frozenset({REL}), FakeClock(1000.0), acct=a,
                         sizes={REL: (SIZE, TOTAL)})  # fmt: skip
    return TestClient(app), log


def _get(c: TestClient, **extra: str) -> int:
    h = {"X-Original-URI": f"/w/{OP}/{REL}", "X-Real-IP": "198.51.100.7",
         "Authorization": "Bearer " + issue_token(SECRET, OP, 5000), **extra}  # fmt: skip
    return c.get("/weights/auth", headers=h).status_code


# ------------------------------------------------------------------ SEC-1


def test_if_range_charges_the_whole_file(tmp_path: Path) -> None:
    c, log = _app(tmp_path)
    log.write_text(f"{OP} 900 200\n")
    assert _get(c, **{"X-Range": "bytes=0-9"}) == 204  # 900 + 10 <= 1320
    # with If-Range nginx may answer 200 with the whole file: charge the whole file
    assert _get(c, **{"X-Range": "bytes=0-9", "X-If-Range": '"bogus"'}) == 403  # 900+1000
    assert _get(c, **{"X-Range": "bytes=0-9", "X-If-Range": ""}) == 204  # empty = absent


def test_conf_forwards_if_range_and_single_range() -> None:
    code = _code()
    assert "proxy_set_header X-If-Range $http_if_range;" in code
    assert "max_ranges 1;" in code


# ------------------------------------------------------------------ SEC-2 / SEC-4 (conf)


def test_acct_log_only_in_gguf_location_for_200_206() -> None:
    code = _code()
    gguf = re.search(r'location ~ "\^/w/.*?\n        \}', code, re.DOTALL)
    assert gguf
    # acct log: only inside the gguf location, conditional on the status map
    assert len(re.findall(r"weights-acct\.log", code)) == 1
    assert re.search(r"access_log\s+\S+weights-acct\.log acct if=\$acct_ok;", gguf.group(0))
    m = re.search(r"map \$status \$acct_ok \{(.*?)\}", code, re.DOTALL)
    assert m and "1;" in m.group(1) and "default 0;" in m.group(1)
    pat = re.search(r'"(~\^[^"]+)"\s+1;', m.group(1))
    assert pat
    rx = re.compile(pat.group(1)[1:])
    assert rx.match("200") and rx.match("206")
    assert not any(rx.match(s) for s in ("401", "403", "404", "416", "500", "503", "2000"))


def test_catch_all_not_logged_and_server_rate_limited() -> None:
    code = _code()
    assert "location / { access_log off; return 404; }" in code
    server = code.split("server {", 1)[1]
    head = server.split("location", 1)[0]
    assert re.search(r"limit_req zone=\w+ burst=\d+ nodelay;", head)  # server level


# ------------------------------------------------------------------ SEC-2 (acct reads)


def test_acct_refresh_reads_in_bounded_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acct_mod, "READ_CHUNK", 64)
    log = tmp_path / "acct.log"
    lines = [f"{OP} 1 206\n"] * 100  # 2000 bytes > many chunks
    log.write_text("".join(lines) + f"{OP} 5")  # partial tail stays unread
    reads: list[int] = []
    real_open = Path.open

    def spy_open(self: Path, *a, **kw):  # type: ignore[no-untyped-def]
        fh = real_open(self, *a, **kw)
        if self == log:
            orig = fh.read

            def read(n: int = -1) -> bytes:
                reads.append(n)
                return orig(n)

            fh.read = read  # type: ignore[method-assign]
        return fh

    monkeypatch.setattr(Path, "open", spy_open)
    a = AcctState(tmp_path / "st", log)
    assert a.served(OP) == 100
    assert reads and all(0 < n <= 64 for n in reads)
    with log.open("a") as f:
        f.write(" 200\n")
    assert a.served(OP) == 105


def test_acct_overlong_line_does_not_stall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acct_mod, "READ_CHUNK", 64)
    log = tmp_path / "acct.log"
    log.write_text("x" * 500 + "\n" + f"{OP} 7 200\n")
    assert AcctState(tmp_path / "st", log).served(OP) == 7


def test_acct_counts_only_200_206(tmp_path: Path) -> None:
    log = tmp_path / "acct.log"
    log.write_text(f"{OP} 100 200\n{OP} 10 206\n{OP} 170 401\n{OP} 170 403\n{OP} 9 416\n")
    assert AcctState(tmp_path / "st", log).served(OP) == 110


# ------------------------------------------------------------------ SEC-8


@pytest.mark.parametrize("meth", ["bind_ip", "served"])
def test_acct_sqlite_error_is_503_fail_closed(tmp_path: Path, meth: str) -> None:
    a = AcctState(tmp_path / "auth", tmp_path / "weights-acct.log")

    def boom(*_a: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    setattr(a, meth, boom)
    c, _ = _app(tmp_path, a)
    assert _get(c) == 503
