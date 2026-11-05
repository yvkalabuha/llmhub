"""T-369 W9: /admin/*."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from api_fixtures import ADMIN_KEY, GAME_KEY, ApiEnv, chat_body
from llmhub.lifecycle_manager.machine import live_instance
from llmhub.persistence import state as cstate

A = {"key": ADMIN_KEY}


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


async def warm_gpu(env: ApiEnv) -> None:
    lease = await env.post("/v1/leases", {"model": "big", "ttl_s": 3600, "prewarm": True})
    assert lease.status_code == 201
    for _ in range(100):
        await env.pump(5)
        inst = live_instance(env.h.conn, "p1")
        if inst is not None and inst["state"] in ("READY", "IDLE"):
            return
    raise AssertionError("GPU did not warm up")


async def test_status_sections(env: ApiEnv) -> None:
    await env.post("/v1/tasks", chat_body(model="localonly"))
    env.worker.hold = True
    await env.post("/v1/tasks", chat_body("b", model="localonly"))
    r = await env.get("/admin/status", **A)
    assert r.status_code == 200
    s = r.json()
    assert s["halted"] is False and s["safe_mode"] is False and s["kill_file"] is False
    assert set(s["pools"]["p1"]) == {"instance", "queue", "leases", "capacity_wait"}
    assert s["pools"]["p1"]["capacity_wait"] is None
    assert s["local"]["backends"]["cpu"]["available"] is True
    assert s["local"]["queue"].get("queued", 0) >= 1
    assert s["budget"]["stage"] == "NORMAL" and "month" in s["budget"] and "day" in s["budget"]
    assert isinstance(s["providers"], list) and isinstance(s["incidents"], list)


async def test_status_shows_instance_and_leases(env: ApiEnv) -> None:
    await warm_gpu(env)
    p = (await env.get("/admin/status", **A)).json()["pools"]["p1"]
    assert p["instance"]["state"] in ("READY", "IDLE") and p["leases"] >= 1


async def test_budget_and_clients(env: ApiEnv) -> None:
    b = (await env.get("/admin/budget", **A)).json()
    assert b["stage"] == "NORMAL" and b["month"]["budget_usd"] == env.cfg.budget.budget_month
    assert isinstance(b["ledger_month"], list)
    c = await env.get("/admin/clients", **A)
    names = {x["name"]: x for x in c.json()["clients"]}
    assert set(names) == {"game", "priv", "low"}
    assert names["game"]["cloud_allowed"] is True and names["game"]["disabled"] is False
    assert "sha256" not in c.text and "key" not in c.text.lower().replace("keys", "")


async def test_hold_release(env: ApiEnv) -> None:
    r = await env.post("/admin/hold", {"model": "big", "ttl_s": 600}, **A)
    assert r.status_code == 201
    lid = r.json()["lease_id"]
    assert (
        env.h.conn.execute("SELECT kind FROM leases WHERE id=?", (lid,)).fetchone()["kind"]
        == "hold"
    )
    rel = await env.post("/admin/release", {"pool": "p1"}, **A)
    assert rel.status_code == 200
    row = env.h.conn.execute("SELECT closed_at FROM leases WHERE id=?", (lid,)).fetchone()
    assert row["closed_at"] is not None


async def test_hold_rejected_by_budget_is_409(env: ApiEnv) -> None:
    r = await env.post("/admin/hold", {"pool": "p1", "ttl_s": 28800}, **A)
    assert r.status_code == 409
    e = r.json()["error"]
    assert e["code"] == "conflict" and e["reason"] == "budget_limit_reached"
    assert env.h.conn.execute("SELECT count(*) FROM leases").fetchone()[0] == 0


async def test_hold_validation(env: ApiEnv) -> None:
    assert (await env.post("/admin/hold", {"pool": "p1"}, **A)).status_code == 400
    assert (await env.post("/admin/hold", {"ttl_s": 60}, **A)).status_code == 400
    assert (await env.post("/admin/hold", {"pool": "zz", "ttl_s": 60}, **A)).status_code == 404
    assert (await env.post("/admin/hold", {"pool": "p1", "ttl_s": 99999}, **A)).status_code in (
        400, 409)  # fmt: skip
    assert (await env.post("/admin/release", {}, **A)).status_code == 400


async def test_kill_destroys_everything_and_halts(env: ApiEnv) -> None:
    await warm_gpu(env)
    assert env.h.provider.store.conn.execute("SELECT count(*) FROM resources").fetchone()[0] >= 1
    r = await env.post("/admin/kill", {"reason": "test"}, pump_s=70, **A)
    assert r.status_code == 200
    report = r.json()
    assert report["completed"] is True and report["remaining"] == [] and report["elapsed_s"] <= 60
    live = env.h.provider.store.conn.execute(
        "SELECT count(*) FROM resources WHERE destroyed_at IS NULL"
    ).fetchone()[0]
    assert live == 0
    assert cstate.is_halted(env.h.conn)


async def test_halted_cloud_only_503_local_200_with_fallback_header(env: ApiEnv) -> None:
    cstate.halt(env.h.conn, "admin", env.clock)
    env.cloud.invalidate()
    r = await env.post("/v1/chat/completions", chat_body(model="cloudonly"))
    assert r.status_code == 503
    e = r.json()["error"]
    assert e["code"] == "controller_halted" and e["retryable"] is False
    ok = await env.post("/v1/chat/completions", chat_body(model="big"), pump_s=3)
    assert ok.status_code == 200, ok.text
    assert ok.headers["X-LLMHub-Fallback"] == "controller_halted"
    assert ok.headers["X-LLMHub-Backend"] == "local:cpu"
    h = (await env.get("/healthz")).json()
    assert h["halted"] is True


async def test_resume_with_kill_file_is_409(env: ApiEnv) -> None:
    cstate.halt(env.h.conn, "admin", env.clock)
    (env.cfg.data_dir / "KILL").write_text("")
    r = await env.post("/admin/resume", {}, **A)
    assert r.status_code == 409
    assert r.json()["error"]["reason"] == "kill_file_present"
    (env.cfg.data_dir / "KILL").unlink()
    ok = await env.post("/admin/resume", {}, **A)
    assert ok.status_code == 200 and ok.json() == {"halted": False}


async def test_resume_after_corruption_needs_reload_billing(env: ApiEnv) -> None:
    cstate.halt(env.h.conn, "db_corrupt", env.clock)
    r = await env.post("/admin/resume", **A)
    assert r.status_code == 409 and r.json()["error"]["reason"] == "reconcile_required"
    assert cstate.is_halted(env.h.conn)
    env.clock.advance_to(env.clock.now() + 5)
    rb = await env.post("/admin/reload_billing", pump_s=5, **A)
    assert rb.status_code == 200, rb.text
    ok = await env.post("/admin/resume", **A)
    assert ok.status_code == 200
    assert not cstate.is_halted(env.h.conn)


async def test_clear_weights_hold(env: ApiEnv) -> None:
    r = await env.post("/admin/clear_weights_hold", {"pool": "p1"}, **A)
    assert r.status_code == 200 and r.json() == {"pool": "p1", "removed": False}
    env.h.conn.execute(
        "INSERT INTO controller_state(key, value, updated_at) VALUES ('weights_hold:p1', ?, 0)",
        (str(env.clock.now() + 600),),
    )
    r2 = await env.post("/admin/clear_weights_hold", {"pool": "p1"}, **A)
    assert r2.json()["removed"] is True
    assert (await env.post("/admin/clear_weights_hold", {"pool": "zz"}, **A)).status_code == 404


async def test_admin_requires_admin_key_everywhere(env: ApiEnv) -> None:
    for method, path in (
        ("GET", "/admin/status"), ("GET", "/admin/budget"), ("GET", "/admin/clients"),
        ("POST", "/admin/reload_clients"), ("POST", "/admin/hold"), ("POST", "/admin/release"),
        ("POST", "/admin/kill"), ("POST", "/admin/resume"), ("POST", "/admin/reload_billing"),
        ("POST", "/admin/clear_weights_hold"),
    ):  # fmt: skip
        r = await env.call(method, path, key=GAME_KEY)
        assert r.status_code == 403, path
        r = await env.call(method, path, key=None)
        assert r.status_code == 401, path
    assert not cstate.is_halted(env.h.conn)
