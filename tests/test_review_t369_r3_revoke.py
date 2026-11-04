"""T-369 review cycle 3 (re-review of fix round 1): SEC-M3 revocation, SEC-L3 reload, FN-2.

The R3 defects are fixed (round 2); the xfail markers were removed."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from api_fixtures import ADMIN_KEY, GAME_KEY, ApiEnv, chat_body
from llmhub.api_gateway import submission
from llmhub.api_gateway.auth import hash_key
from llmhub.configuration.loader import ConfigError
from llmhub.task_queue import local, queue


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path, config_path=tmp_path / "cfg.yaml")
    yield e
    await e.aclose()


def _write(env: ApiEnv, **edit: Any) -> Path:
    path = env.rt.config_path
    assert path is not None
    raw: dict[str, Any] = env.cfg.model_dump(mode="json")
    if "game_cloud" in edit:
        raw["clients"]["game"]["cloud_allowed"] = edit["game_cloud"]
    if "admin" in edit:
        raw["api"]["admin_key_sha256"] = edit["admin"]
    if "game_hash" in edit:
        raw["clients"]["game"]["key_sha256"] = edit["game_hash"]
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _queued_cloud(env: ApiEnv) -> list[Any]:
    return env.jobs("client_id='game' AND pool IS NOT NULL AND status IN ('submitted','queued')")


# --- SEC-M3 residual gaps ------------------------------------------------------------------


async def test_boot_sync_revokes_queued_cloud_jobs_of_client_without_cloud(env: ApiEnv) -> None:
    jid = (await env.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
    assert env.job(jid)["pool"] == "p1"
    # restart with an edited config: cloud_allowed false (the process re-runs sync())
    env.cfg.clients["game"] = env.cfg.clients["game"].model_copy(update={"cloud_allowed": False})
    env.registry.sync()
    assert env.job(jid)["status"] == "cancelled"


async def test_running_cloud_job_of_revoked_client_does_not_return_to_cloud_queue(
    env: ApiEnv,
) -> None:
    jid = (await env.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
    now = env.clock.now()
    with env.h.conn:
        env.h.conn.execute(
            "UPDATE jobs SET status='running', worker_id='boot-x', worker_heartbeat_at=?,"
            " started_at=?, attempts=1 WHERE id=?",
            (now, now, jid),
        )
    env.registry.reload(_write(env, game_cloud=False))
    queue.requeue(env.h.conn, jid, now, count_attempt=False, reason="restart")
    row = env.job(jid)
    assert not (row["status"] == "queued" and row["pool"] is not None)


async def test_reload_during_submit_does_not_queue_cloud_job(
    env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    real = submission.detect_secrets

    def slow_detect(*a: Any, **k: Any) -> Any:
        entered.set()
        release.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(submission, "detect_secrets", slow_detect)
    path = _write(env, game_cloud=False)
    task = asyncio.ensure_future(env.post("/v1/tasks", chat_body("q", model="cloudonly")))
    for _ in range(500):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()
    env.registry.reload(path)  # SIGHUP arrives while the request is inside the detector
    release.set()
    r = await task
    assert r.status_code in (202, 403), r.text
    assert _queued_cloud(env) == []


async def test_revoke_closes_explicit_lease_but_not_other_clients(env: ApiEnv) -> None:
    lid = (await env.post("/v1/leases", {"model": "big", "ttl_s": 300})).json()["id"]
    jid = (await env.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
    # a reload that changes nothing for `game` revokes nothing
    env.registry.reload(_write(env))
    assert env.job(jid)["status"] == "queued"
    lease = env.h.conn.execute("SELECT closed_at FROM leases WHERE id=?", (lid,)).fetchone()
    assert lease["closed_at"] is None
    woke: list[int] = []
    env.registry.on_revoke = lambda: woke.append(1)
    env.registry.reload(_write(env, game_cloud=False))
    assert env.job(jid)["status"] == "cancelled" and woke == [1]
    # lease renew of the revoked client is refused, and the queue no longer takes cloud work
    r = await env.post(f"/v1/leases/{lid}/renew", {})
    assert r.status_code == 403 and r.json()["error"]["reason"] == "cloud_not_allowed"
    r = await env.post("/v1/tasks", chat_body("q2", model="cloudonly"))
    assert _queued_cloud(env) == [], r.text


# --- SEC-L3 ---------------------------------------------------------------------------------


async def test_reload_rejects_client_hash_equal_to_new_admin_hash_nothing_changes(
    env: ApiEnv,
) -> None:
    new_admin = hash_key("lh_new_admin_key_00000000")
    before = env.cfg.api.admin_key_sha256
    with pytest.raises(ConfigError):
        env.registry.reload(_write(env, admin=new_admin, game_hash=new_admin, game_cloud=False))
    assert env.cfg.api.admin_key_sha256 == before
    p = env.registry.by_key(GAME_KEY)
    assert p is not None and p.cloud_allowed
    assert (await env.get("/admin/status", key=ADMIN_KEY)).status_code == 200


async def test_old_admin_key_never_becomes_admin_after_rotation_even_as_client(
    env: ApiEnv,
) -> None:
    new_key = "lh_rotated_admin_000000000"
    env.registry.reload(_write(env, admin=hash_key(new_key)))
    # second reload: the old admin key is now registered as a client key
    env.registry.reload(_write(env, admin=hash_key(new_key), game_hash=hash_key(ADMIN_KEY)))
    assert (await env.get("/admin/status", key=ADMIN_KEY)).status_code == 403
    assert (await env.get("/admin/status", key=new_key)).status_code == 200
    assert (await env.get("/v1/models", key=ADMIN_KEY)).status_code == 200


# --- FN-2 -----------------------------------------------------------------------------------


async def test_retry_same_key_private_header_never_reuses_failed_cloud_job(env: ApiEnv) -> None:
    h = {"Idempotency-Key": "k-r3"}
    body = chat_body("q", model="cloudonly")
    jid = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    assert env.job(jid)["pool"] == "p1"
    with env.h.conn:
        env.h.conn.execute(
            "UPDATE jobs SET status='failed', error_code='cloud_unavailable',"
            " fallback_reason='provider_api_error', finished_at=? WHERE id=?",
            (env.clock.now(), jid),
        )
    # same key + private header: refused (cloud-only alias) or 409 (other canonical body);
    # the failed cloud job is neither reused nor stripped of its key
    r = await env.post("/v1/tasks", body, headers={**h, "X-LLMHub-Data-Class": "private"})
    assert r.status_code in (403, 409), r.text
    assert env.job(jid)["idem_key"] == "k-r3"
    n_before = len(env.jobs())
    # same body: exactly one new job; a further retry joins it (no loop, no second job)
    first = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    second = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    assert first != jid and first == second
    assert len(env.jobs()) == n_before + 1


async def test_retry_chain_stops_at_non_retryable_failure(env: ApiEnv) -> None:
    env.worker.hold = True
    h = {"Idempotency-Key": "k-chain"}
    body = chat_body(model="localonly")
    a = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    local.fail(env.h.conn, a, "upstream_timeout", env.clock.now())
    b = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    assert b != a
    local.fail(env.h.conn, b, "context_too_large", env.clock.now())
    r = await env.post("/v1/tasks", body, headers=h)
    assert r.status_code == 200 and r.json()["id"] == b
    assert [j["idem_key"] for j in env.jobs("client_id='game'")] == [None, "k-chain"]
