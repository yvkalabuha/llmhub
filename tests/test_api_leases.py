"""T-369 W8: /v1/leases."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from api_fixtures import LOW_KEY, PRIV_KEY, ApiEnv, chat_body
from llmhub.lifecycle_manager.machine import live_instance


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


async def test_create_renew_close(env: ApiEnv) -> None:
    r = await env.post("/v1/leases", {"model": "big", "ttl_s": 120})
    assert r.status_code == 201
    lease = r.json()
    assert lease["pool"] == "p1" and lease["kind"] == "explicit" and lease["ttl_s"] == 120
    assert lease["expires_at"] == pytest.approx(env.clock.now() + 120)
    await env.pump(60)
    renewed = await env.post(f"/v1/leases/{lease['id']}/renew")
    assert renewed.status_code == 200
    assert renewed.json()["expires_at"] == pytest.approx(env.clock.now() + 120)
    closed = await env.post(f"/v1/leases/{lease['id']}/close", {"final": True})
    assert closed.status_code == 200 and closed.json() == {"id": lease["id"], "closed": True}
    again = await env.post(f"/v1/leases/{lease['id']}/close")  # idempotent
    assert again.status_code == 200
    row = env.h.conn.execute("SELECT final FROM leases WHERE id=?", (lease["id"],)).fetchone()
    assert row["final"] == 1
    gone = await env.post(f"/v1/leases/{lease['id']}/renew")
    assert gone.status_code == 409 and gone.json()["error"]["code"] == "lease_closed"


async def test_default_ttl_and_prewarm_kind(env: ApiEnv) -> None:
    r = await env.post("/v1/leases", {"model": "big", "prewarm": True})
    assert r.json()["kind"] == "prewarm" and r.json()["ttl_s"] == env.cfg.leases.default_ttl_s


async def test_expired_renew_is_410(env: ApiEnv) -> None:
    lease = (await env.post("/v1/leases", {"model": "big", "ttl_s": 30})).json()
    await env.pump(31)
    r = await env.post(f"/v1/leases/{lease['id']}/renew")
    assert r.status_code == 410 and r.json()["error"]["code"] == "lease_expired"
    # before the controller closes it: expiry by the clock alone
    lease2 = (await env.post("/v1/leases", {"model": "big", "ttl_s": 30})).json()
    env.clock.advance_to(env.clock.now() + 31)
    r2 = await env.post(f"/v1/leases/{lease2['id']}/renew")
    assert r2.status_code == 410


@pytest.mark.parametrize("ttl", [10, 29, 3601, 100000, -5])
async def test_ttl_out_of_range_is_400(env: ApiEnv, ttl: int) -> None:
    r = await env.post("/v1/leases", {"model": "big", "ttl_s": ttl})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


async def test_ttl_bounds_ok(env: ApiEnv) -> None:
    assert (await env.post("/v1/leases", {"model": "big", "ttl_s": 30})).status_code == 201
    assert (await env.post("/v1/leases", {"model": "big", "ttl_s": 3600})).status_code == 201


async def test_private_client_403_unknown_model_404_no_pool_404(env: ApiEnv) -> None:
    r = await env.post("/v1/leases", {"model": "big"}, key=PRIV_KEY)
    assert r.status_code == 403 and r.json()["error"]["reason"] == "cloud_not_allowed"
    assert (await env.post("/v1/leases", {"model": "nope"})).status_code == 404
    no_pool = await env.post("/v1/leases", {"model": "localonly"})
    assert no_pool.status_code == 404 and no_pool.json()["error"]["code"] == "not_found"
    denied = await env.post("/v1/leases", {"model": "big"}, key=LOW_KEY)
    assert denied.status_code == 403  # not in allowed_models
    assert (await env.post("/v1/leases", {"ttl_s": 60})).status_code == 404
    bad = await env.post("/v1/leases", {"model": "big", "prewarm": "yes"})
    assert bad.status_code == 400
    assert env.h.conn.execute("SELECT count(*) FROM leases").fetchone()[0] == 0


async def test_foreign_lease_is_404(env: ApiEnv) -> None:
    lease = (await env.post("/v1/leases", {"model": "big", "ttl_s": 60})).json()
    for path in ("renew", "close"):
        r = await env.post(f"/v1/leases/{lease['id']}/{path}", key=LOW_KEY)
        assert r.status_code == 404
    assert (await env.post("/v1/leases/zzz/renew")).status_code == 404


async def test_implicit_lease_not_reachable(env: ApiEnv) -> None:
    await env.post("/v1/tasks", chat_body(model="cloudonly"))
    row = env.h.conn.execute("SELECT id FROM leases WHERE kind='implicit'").fetchone()
    assert (await env.post(f"/v1/leases/{row['id']}/renew")).status_code == 404


async def test_prewarm_starts_gpu_and_silent_agent_keeps_it_alive(env: ApiEnv) -> None:
    lease = (await env.post("/v1/leases", {"model": "big", "ttl_s": 3600, "prewarm": True})).json()
    for _ in range(100):
        await env.pump(5)
        inst = live_instance(env.h.conn, "p1")
        if inst is not None and inst["state"] in ("READY", "IDLE"):
            break
    inst = live_instance(env.h.conn, "p1")
    assert inst is not None and inst["state"] in ("READY", "IDLE")
    iid = inst["id"]
    await env.pump(20 * 60)  # the agent is silent for 20 minutes (no renew)
    inst = live_instance(env.h.conn, "p1")
    assert inst is not None and inst["id"] == iid and inst["state"] in ("READY", "IDLE")
    await env.post(f"/v1/leases/{lease['id']}/close", {"final": True})
    await env.pump(900)
    after = live_instance(env.h.conn, "p1")
    assert after is None or after["state"] not in ("READY", "IDLE", "BUSY")
