"""Review T-371 cycle 2 (correctness & reliability): backup under WAL writers, integrity with a
live writer, lazy llama key vs probes, /slots robustness, e2e kill semantics, api.host.

xfail(strict=True) marks a confirmed defect: the test turns green once it is fixed."""

from __future__ import annotations

import argparse
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from fake_llama import FakeLlama
from llmhub.clock import FakeClock
from llmhub.configuration.api_models import ApiConfig, LocalBackendConfig
from llmhub.local_inference.client import (
    KEY_RELOAD_MIN_S,
    LocalBackendClient,
    file_key_source,
)
from llmhub.persistence.backup import backup_state_db
from llmhub.persistence.db import connect, migrate
from llmhub.persistence.integrity import open_state_db

KEY1 = "llama-key-one-0000000000"
KEY2 = "llama-key-two-1111111111"


# ------------------------------------------------------------------ backup under WAL writers
def _wal_db(path: Path) -> None:
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE a(v INTEGER)")
    c.execute("CREATE TABLE b(v INTEGER)")
    c.commit()
    c.close()


def test_backup_is_consistent_with_live_wal_writer(tmp_path: Path) -> None:
    src = tmp_path / "state.db"
    _wal_db(src)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        c = sqlite3.connect(src, timeout=5, isolation_level=None)
        try:
            i = 0
            while not stop.is_set():
                c.execute("BEGIN IMMEDIATE")
                c.execute("INSERT INTO a VALUES (?)", (i,))
                c.execute("INSERT INTO b VALUES (?)", (i,))
                c.execute("COMMIT")
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        outs = []
        for k in range(5):
            now = datetime(2026, 11, 4, 12, 0, k, tzinfo=UTC)
            outs.append(backup_state_db(src, tmp_path / "bk", keep=10, now=now))
    finally:
        stop.set()
        t.join(10)
    assert not errors
    # only finished copies: no *.tmp, no -wal/-shm/-journal left next to them
    names = sorted(p.name for p in (tmp_path / "bk").iterdir())
    assert all(n.startswith("state-") and n.endswith(".db") for n in names), names
    assert {oct(p.stat().st_mode & 0o777) for p in (tmp_path / "bk").iterdir()} == {"0o640"}
    for out in outs:
        c = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
        try:
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            na = c.execute("SELECT count(*) FROM a").fetchone()[0]
            nb = c.execute("SELECT count(*) FROM b").fetchone()[0]
        finally:
            c.close()
        assert na == nb  # one snapshot, never half a transaction


# ------------------------------------------------------------------ integrity: normal writer
def test_open_with_reaper_holding_write_tx_does_not_quarantine(tmp_path: Path) -> None:
    """A RESERVED (write) lock of a live writer is normal under WAL: open waits or succeeds,
    the file is never renamed."""
    path = tmp_path / "state.db"
    c = connect(path)
    migrate(c)
    c.close()
    holder = connect(path)
    holder.execute("BEGIN IMMEDIATE")
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        if len(slept) == 2:
            holder.execute("COMMIT")

    try:
        res = open_state_db(path, FakeClock(), busy_wait_s=10, sleep=sleep)
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()
    assert path.exists() and not list(tmp_path.glob("*.corrupt-*"))
    assert res.safe_mode is False
    res.conn.close()


# ------------------------------------------------------------------ lazy key vs probes
def _key(tmp: Path, text: str) -> Path:
    p = tmp / "api-key"
    p.write_text(text + "\n")
    p.chmod(0o640)
    return p


def _cfg(path: Path | None, probe: str = "slots") -> LocalBackendConfig:
    return LocalBackendConfig(
        url="http://fake-llama/v1", model="m", ctx_tokens=8192, pp_tps=100, tg_tps=5,
        api_key_file=path, probe=probe,  # type: ignore[arg-type]
    )  # fmt: skip



async def test_probe_401_after_key_rotation_rereads_key(tmp_path: Path) -> None:
    """llama-server protects /slots with --api-key. After a key rotation every background
    probe gets 401 -> backend marked unavailable -> no chat is dispatched -> the chat-only
    re-read after 401 never runs: the backend stays down until the API restarts."""
    path = _key(tmp_path, KEY1)
    fake = FakeLlama(api_key=KEY1)
    clock = FakeClock()
    c = LocalBackendClient("cpu", _cfg(path), None, http=fake.client(), clock=clock,
                           key_source=file_key_source(path))  # fmt: skip
    assert (await c.probe()).ok is True
    fake.api_key = KEY2
    _key(tmp_path, KEY2)
    oks = []
    for _ in range(3):  # several probe intervals later
        clock.advance_to(clock.now() + KEY_RELOAD_MIN_S + 1)
        oks.append((await c.probe()).ok)
    assert oks[-1] is True



@pytest.mark.parametrize("body", [{"error": {"message": "x"}}, "abc"])
async def test_slots_non_list_json_is_not_healthy(body: Any) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = LocalBackendClient("cpu", _cfg(None), None, http=http, clock=FakeClock())
    r = await c.probe()
    await http.aclose()
    assert r.ok is False


# ------------------------------------------------------------------ e2e kill semantics
def _load_e2e() -> Any:
    import importlib.util
    import sys

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("e2e_mock_rev2", root / "scripts/e2e_mock.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["e2e_mock_rev2"] = mod
    spec.loader.exec_module(mod)
    return mod



def test_e2e_kill_fails_when_kill_report_has_remaining_resources() -> None:
    """After kill the instance row is PENDING_DELETE (destroy not confirmed): /admin/status
    hides it (LIVE_IN_INDEX), so `zero_resources_le_60s` passes although the provider
    resource is alive. The phase must fail on `remaining` / `completed=false`."""
    e2e = _load_e2e()
    state = {"killed": False}

    def resp(code: int, data: Any) -> httpx.Response:
        return httpx.Response(code, json=data, request=httpx.Request("GET", "http://x"))

    class FakeApi(e2e.Api):  # type: ignore[misc, name-defined]
        def req(self, method: str, path: str, **kw: Any) -> httpx.Response:
            if path == "/v1/leases":
                return resp(201, {"id": "L1"})
            return resp(200, {})

        def admin(self, method: str, path: str, body: Any = None) -> httpx.Response:
            if path == "/admin/status":
                inst = None if state["killed"] else {"state": "BOOTING"}
                return resp(200, {"pools": {"p": {"instance": inst}}})
            if path == "/admin/kill":
                state["killed"] = True
                return resp(200, {"completed": False, "remaining": ["mock-r1"],
                                  "destroyed": [], "stopped_rows": ["i1"], "elapsed_s": 45.0})
            if path == "/admin/resume":
                return resp(200, {"halted": False})
            return resp(404, {})

    api = FakeApi("http://x", "k", "a", 5)
    rep = e2e.Report("kill")
    e2e.phase_kill(api, argparse.Namespace(cloud_model="m"), rep)
    assert rep.ok is False


# ------------------------------------------------------------------ api.host
@pytest.mark.parametrize("host", ["::ffff:0.0.0.0", "0.0.0.0", "::", "[::]", " 0.0.0.0 ", ""])
def test_api_host_rejects_wildcards_incl_v4_mapped(host: str) -> None:
    with pytest.raises(ValueError):
        ApiConfig(host=host)


# ------------------------------------------------------------------ 429 limit vs retried key
async def test_idem_retry_of_failed_job_is_limited(tmp_path: Path) -> None:
    from api_fixtures import PRIV_KEY, ApiEnv, api_config, chat_body
    from llmhub.api_gateway.auth import hash_key
    from llmhub.configuration.api_models import ClientConfig

    cfg = api_config(tmp_path)
    cfg.clients["priv"] = ClientConfig(
        key_sha256=hash_key(PRIV_KEY), allowed_models=["*"], max_active_jobs=2
    )
    env = ApiEnv(tmp_path, cfg=cfg)
    try:
        hdr = {"Idempotency-Key": "k-retry"}
        first = await env.post("/v1/tasks", chat_body("a"), key=PRIV_KEY, headers=hdr)
        assert first.status_code == 202
        env.rt.conn.execute(
            "UPDATE jobs SET status='failed', error_code='local_unavailable' WHERE id=?",
            (first.json()["id"],),
        )
        for i in range(2):
            r = await env.post("/v1/tasks", chat_body(f"q{i}"), key=PRIV_KEY)
            assert r.status_code == 202
        again = await env.post("/v1/tasks", chat_body("a"), key=PRIV_KEY, headers=hdr)
        active = env.rt.conn.execute(
            "SELECT count(*) FROM jobs WHERE status IN ('submitted','queued','running')"
        ).fetchone()[0]
        assert again.status_code == 429 and active == 2, (again.status_code, active)
    finally:
        await env.aclose()
