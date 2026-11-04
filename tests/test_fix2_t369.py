"""T-369 fix round 2: boot-time revocation (R3-1), requeue of a revoked running cloud job
(R3-2), rights re-check before submit (R3-3), SecretMaskFilter never raises (R3-4)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from api_fixtures import ApiEnv, chat_body
from llmhub.api_gateway.logging import SecretMaskFilter
from llmhub.task_queue import queue


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


def _drop_cloud(env: ApiEnv) -> None:
    env.cfg.clients["game"] = env.cfg.clients["game"].model_copy(update={"cloud_allowed": False})


async def test_boot_sync_closes_explicit_lease_tags_job_and_wakes(env: ApiEnv) -> None:
    lid = (await env.post("/v1/leases", {"model": "big", "ttl_s": 300})).json()["id"]
    jid = (await env.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
    woke: list[int] = []
    env.registry.on_revoke = lambda: woke.append(1)
    _drop_cloud(env)
    assert env.registry.sync() == ["game"]
    assert woke == [1]
    row = env.job(jid)
    assert row["status"] == "cancelled" and row["error_code"] == queue.CLOUD_NOT_ALLOWED
    lease = env.h.conn.execute("SELECT close_reason FROM leases WHERE id=?", (lid,)).fetchone()
    assert lease["close_reason"] == "revoked"
    err = (await env.get(f"/v1/tasks/{jid}")).json()["error"]
    assert err["code"] == "cancelled" and err["reason"] == "cloud_not_allowed"
    # idempotent: a second sync finds nothing to revoke and does not wake again
    assert env.registry.sync() == [] and woke == [1]


async def test_boot_sync_revokes_removed_client(env: ApiEnv) -> None:
    jid = (await env.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
    del env.cfg.clients["game"]
    assert env.registry.sync() == ["game"]
    assert env.job(jid)["status"] == "cancelled"


async def test_boot_sync_keeps_cloud_jobs_of_allowed_clients(env: ApiEnv) -> None:
    jid = (await env.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
    assert env.registry.sync() == []
    assert env.job(jid)["status"] == "queued"


async def test_requeue_of_allowed_client_running_cloud_job_still_requeues(env: ApiEnv) -> None:
    jid = (await env.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
    now = env.clock.now()
    with env.h.conn:
        env.h.conn.execute(
            "UPDATE jobs SET status='running', worker_id='boot-x', started_at=?, attempts=1"
            " WHERE id=?",
            (now, jid),
        )
    assert queue.requeue(env.h.conn, jid, now, count_attempt=False, reason="restart") == "queued"
    assert env.job(jid)["pool"] == "p1"


async def test_requeue_of_revoked_running_cloud_job_cancels_with_reason(env: ApiEnv) -> None:
    jid = (await env.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
    now = env.clock.now()
    with env.h.conn:
        env.h.conn.execute(
            "UPDATE jobs SET status='running', worker_id='boot-x', started_at=?, attempts=1"
            " WHERE id=?",
            (now, jid),
        )
    _drop_cloud(env)
    env.registry.sync()  # running job is not touched by revocation (its GPU is paid)
    assert env.job(jid)["status"] == "running"
    assert queue.requeue(env.h.conn, jid, now, count_attempt=False, reason="restart") == "failed"
    row = env.job(jid)
    assert row["status"] == "cancelled" and row["error_code"] == queue.CLOUD_NOT_ALLOWED
    assert row["pool"] == "p1"  # never moved to the local queue either


async def test_submit_refused_when_client_vanished_before_controller_submit(
    env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.registry, "by_id", lambda _cid: None)
    r = await env.post("/v1/tasks", chat_body("q", model="cloudonly"))
    assert r.status_code == 403 and r.json()["error"]["reason"] == "cloud_not_allowed"
    assert env.jobs("pool IS NOT NULL") == []


async def test_local_submit_not_affected_by_rights_recheck(
    env: ApiEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.registry, "by_id", lambda _cid: None)
    r = await env.post("/v1/tasks", chat_body("q", model="localonly"))
    assert r.status_code == 202, r.text


def _record(msg: object, args: object) -> logging.LogRecord:
    return logging.LogRecord("llmhub.t", logging.ERROR, __file__, 1, msg, args, None)  # type: ignore[arg-type]


def test_mask_filter_bad_format_keeps_masking() -> None:
    rec = _record("token=sekrit-value %(missing)s", ({"other": 1},))
    assert SecretMaskFilter().filter(rec) is True
    text = logging.Formatter().format(rec)
    assert "sekrit-value" not in text


def test_mask_filter_unexpected_error_drops_record_but_returns_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_text: str) -> str:
        raise RuntimeError("mask broke")

    monkeypatch.setattr("llmhub.api_gateway.logging.mask", boom)
    rec = _record("secret=abc", None)
    assert SecretMaskFilter().filter(rec) is True
    assert rec.getMessage() == "<log record dropped by mask filter>"
    assert rec.exc_info is None and rec.exc_text is None
