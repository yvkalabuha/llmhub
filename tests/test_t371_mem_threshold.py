"""T-371 R2 (F6): the weights validator refuses (503 low_memory) below host MemAvailable."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from llmhub.clock import FakeClock
from llmhub.persistence.db import connect, migrate
from llmhub.weights.acct import AcctState
from llmhub.weights.auth_app import build_auth_app, mem_available_mib
from llmhub.weights.tokens import issue_token

SECRET = b"w" * 32
OP = "0123456789ab"
REL = "qwen/model.gguf"

MEMINFO = """MemTotal:       65000000 kB
MemFree:         1000000 kB
MemAvailable:   {kb} kB
Buffers:          100000 kB
"""


def _meminfo(tmp: Path, gib: float) -> Path:
    p = tmp / "meminfo"
    p.write_text(MEMINFO.format(kb=int(gib * 1024 * 1024)))
    return p


def test_parse_meminfo(tmp_path: Path) -> None:
    assert mem_available_mib(_meminfo(tmp_path, 4)) == 4096
    assert mem_available_mib(tmp_path / "missing") is None
    bad = tmp_path / "bad"
    bad.write_text("MemTotal: 1 kB\n")
    assert mem_available_mib(bad) is None


def test_real_proc_meminfo_parses() -> None:
    if not Path("/proc/meminfo").exists():
        pytest.skip("no /proc/meminfo")
    v = mem_available_mib()
    assert v is not None and v > 0


def _client(tmp: Path, meminfo: Path, threshold: int = 8192) -> TestClient:
    db = tmp / "state.db"
    conn = connect(db)
    migrate(conn)
    conn.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since, created_at,"
        " max_lifetime_at) VALUES ('i', ?, 'p', 'mock', 'l', 'LOADING_MODEL', 0, 0, 1e12)", (OP,)
    )  # fmt: skip
    conn.close()
    log = tmp / "acct.log"
    log.write_text("")
    app = build_auth_app(
        db, SECRET, frozenset({REL}), FakeClock(1000.0), acct=AcctState(tmp / "auth", log),
        sizes={REL: (1000, 1100)}, min_mem_available_mib=threshold, meminfo=meminfo,
    )  # fmt: skip
    return TestClient(app, client=("127.0.0.1", 5000))


def _get(c: TestClient, token: bool = True) -> int:
    h = {"X-Original-URI": f"/w/{OP}/{REL}", "X-Real-IP": "198.51.100.7"}
    if token:
        h["Authorization"] = "Bearer " + issue_token(SECRET, OP, 5000)
    return c.get("/weights/auth", headers=h).status_code


def _metrics(c: TestClient) -> str:
    return c.get("/metrics").text


def test_low_memory_503_and_metric(tmp_path: Path) -> None:
    c = _client(tmp_path, _meminfo(tmp_path, 4))
    assert _get(c) == 503
    assert 'verdict="low_memory"} 1' in _metrics(c)
    assert _get(c, token=False) == 401  # unauthenticated noise is not told about memory


def test_enough_memory_normal_check(tmp_path: Path) -> None:
    c = _client(tmp_path, _meminfo(tmp_path, 31))
    assert _get(c) == 204
    assert _get(c, token=False) == 401
    assert "low_memory" not in _metrics(c)


def test_threshold_zero_disables(tmp_path: Path) -> None:
    c = _client(tmp_path, _meminfo(tmp_path, 1), threshold=0)
    assert _get(c) == 204


def test_unreadable_meminfo_does_not_block(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    c = _client(tmp_path, tmp_path / "no-such-meminfo")
    with caplog.at_level(logging.WARNING, logger="llmhub.weights.auth"):
        assert _get(c) == 204
        assert _get(c) == 204
    assert caplog.text.count("MemAvailable") == 1  # warned once
