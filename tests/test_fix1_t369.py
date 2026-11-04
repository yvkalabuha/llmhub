"""T-369 fix round 1: regression tests beyond the reviewers' (formerly xfail) tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from api_fixtures import ADMIN_KEY, GAME_KEY, ApiEnv, chat_body
from h_fixtures import LocalEnv
from int_fixtures import IntEnv
from llmhub.api_gateway import auth
from llmhub.api_gateway.app import _supervise  # pyright: ignore[reportPrivateUsage]
from llmhub.api_gateway.auth import hash_key
from llmhub.api_gateway.logging import SecretMaskFilter, mask
from llmhub.budget_manager import reservations
from llmhub.model_router.secrets import detect_secrets
from llmhub.persistence.db import Busy
from llmhub.task_queue import local


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


# --- FN-1 ----------------------------------------------------------------------------------


async def test_retryable_error_says_should_retry_true(env: ApiEnv) -> None:
    env.worker.hold = True
    jid = (await env.post("/v1/tasks", chat_body(model="localonly"))).json()["id"]
    r = await env.get(f"/v1/tasks/{jid}/result")
    assert r.status_code == 409 and r.json()["error"]["code"] == "task_not_ready"
    assert r.headers["x-should-retry"] == "true"
    r = await env.get("/v1/models", key=None)
    assert r.status_code == 401 and r.headers["x-should-retry"] == "false"


# --- FN-2 ----------------------------------------------------------------------------------


async def test_same_key_after_non_retryable_failure_returns_the_old_job(env: ApiEnv) -> None:
    env.worker.hold = True
    h = {"Idempotency-Key": "k-final"}
    body = chat_body(model="localonly")
    jid = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    local.fail(env.h.conn, jid, "deadline_exceeded", env.clock.now())
    again = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    assert again == jid
    # a cancelled job is final too
    h2 = {"Idempotency-Key": "k-cancel"}
    jid2 = (await env.post("/v1/tasks", body, headers=h2)).json()["id"]
    await env.post(f"/v1/tasks/{jid2}/cancel")
    assert (await env.post("/v1/tasks", body, headers=h2)).json()["id"] == jid2


async def test_same_key_after_retryable_failure_moves_the_key(env: ApiEnv) -> None:
    env.worker.hold = True
    h = {"Idempotency-Key": "k-move"}
    body = chat_body(model="localonly")
    jid = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    local.fail(env.h.conn, jid, "local_unavailable", env.clock.now())
    new = (await env.post("/v1/tasks", body, headers=h)).json()["id"]
    assert new != jid
    assert env.job(jid)["idem_key"] is None and env.job(new)["idem_key"] == "k-move"
    # the key now points at the new job: a third call joins it
    assert (await env.post("/v1/tasks", body, headers=h)).json()["id"] == new


# --- FN-3 ----------------------------------------------------------------------------------


async def test_worker_loop_survives_any_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = LocalEnv(tmp_path)
    w = e.worker
    probes = 0
    stop = asyncio.Event()

    async def bad_probe() -> None:
        nonlocal probes
        probes += 1
        if probes == 1:
            raise RuntimeError("boom")
        stop.set()

    monkeypatch.setattr(w, "probe_all", bad_probe)
    w._probe_at = -1e9  # pyright: ignore[reportPrivateUsage]
    task = asyncio.ensure_future(w.run_forever(stop))
    for _ in range(200):
        await asyncio.sleep(0)
        if task.done():
            break
        nxt = e.clock.next_waiter()
        if nxt is not None:
            e.clock.advance_to(nxt)
        w._probe_at = -1e9  # pyright: ignore[reportPrivateUsage]
    if not task.done():
        stop.set()
        e.clock.advance_to(e.clock.now() + 3600)
        await asyncio.wait_for(task, 5)
    assert task.exception() is None
    assert probes >= 2


# --- controller/worker supervision ---------------------------------------------------------


async def test_dead_background_loop_is_fatal(env: ApiEnv) -> None:
    calls: list[str] = []
    env.rt.on_fatal = calls.append
    stop = asyncio.Event()

    async def crash() -> None:
        raise RuntimeError("x")

    t = asyncio.create_task(crash(), name="controller")
    t.add_done_callback(_supervise(env.rt, stop))
    await asyncio.gather(t, return_exceptions=True)
    await asyncio.sleep(0)
    assert calls == ["controller"]

    # a loop that ends after stop (normal shutdown) or is cancelled is not fatal
    stop.set()
    t2 = asyncio.create_task(asyncio.sleep(0), name="local_worker")
    t2.add_done_callback(_supervise(env.rt, stop))
    await t2
    stop.clear()
    t3 = asyncio.create_task(asyncio.sleep(10), name="waiter")
    t3.add_done_callback(_supervise(env.rt, stop))
    t3.cancel()
    await asyncio.gather(t3, return_exceptions=True)
    await asyncio.sleep(0)
    assert calls == ["controller"]


# --- SEC-M1 --------------------------------------------------------------------------------


def test_mask_filter_masks_stack_info_and_detector_patterns() -> None:
    token = "ghs_" + "b" * 36
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "msg %s", (token,), None)
    record.stack_info = f"Stack (most recent call last):\n  url=https://u:pw{token}@h/"
    assert SecretMaskFilter().filter(record)
    text = logging.Formatter().format(record)
    assert token not in text and "pw" + token not in text
    assert "sk-" + "Z" * 24 not in mask("key sk-" + "Z" * 24)
    assert "AKIA" + "A" * 16 not in mask("AKIA" + "A" * 16)


def test_mask_filter_is_idempotent_over_several_handlers() -> None:
    try:
        raise ValueError("Bearer " + "q" * 30)
    except ValueError as exc:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "e", None,
                                   (type(exc), exc, exc.__traceback__))  # fmt: skip
    for _ in range(2):
        assert SecretMaskFilter().filter(record)
    text = logging.Formatter().format(record)
    assert "Traceback" in text and "q" * 30 not in text


# --- SEC-M2 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "s",
    ["rk_live_" + "a" * 20, "sk_test_" + "a" * 20, "ghu_" + "a" * 36, "ghr_" + "a" * 36,
     "npm_" + "a" * 36, "xoxe-" + "a" * 20],
)  # fmt: skip
def test_more_token_formats_are_detected(s: str) -> None:
    assert detect_secrets({"messages": [{"content": f"x {s}"}]})


def test_nested_dict_key_is_scanned() -> None:
    assert detect_secrets({"a": {"b": [{"sk-" + "A" * 24: {"c": None}}]}})
    assert detect_secrets({"a": {"ok": 1}}) == []


# --- SEC-L1 --------------------------------------------------------------------------------


async def test_duplicate_data_class_header_without_private_stays_cloud(env: ApiEnv) -> None:
    import json

    body = json.dumps(chat_body("d", model="cloudonly")).encode()
    headers = [
        ("Authorization", f"Bearer {GAME_KEY}"), ("Content-Type", "application/json"),
        ("X-LLMHub-Data-Class", "cloud_allowed"), ("X-LLMHub-Data-Class", "cloud_allowed"),
    ]  # fmt: skip
    r = await env.client.post("/v1/tasks", content=body, headers=httpx.Headers(headers))
    assert r.status_code == 202, r.text
    bad = [*headers[:2], ("X-LLMHub-Data-Class", "cloud_allowed"), ("X-LLMHub-Data-Class", "x")]
    r = await env.client.post("/v1/tasks", content=body, headers=httpx.Headers(bad))
    assert r.status_code == 400


# --- SEC-M3 / SEC-L3 -----------------------------------------------------------------------


def _dump(env: ApiEnv, path: Path, **edit: Any) -> dict[str, Any]:
    raw: dict[str, Any] = env.cfg.model_dump(mode="json")
    for k, v in edit.items():
        raw[k] = v
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return raw


async def test_removed_client_loses_queued_cloud_jobs_and_leases(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    e = ApiEnv(tmp_path, config_path=cfg_path)
    try:
        jid = (await e.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
        lid = (await e.post("/v1/leases", {"model": "big", "ttl_s": 300})).json()["id"]
        raw = e.cfg.model_dump(mode="json")
        del raw["clients"]["game"]
        cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        assert (await e.post("/admin/reload_clients", key=ADMIN_KEY)).status_code == 200
        assert e.job(jid)["status"] == "cancelled"
        lease = e.h.conn.execute("SELECT * FROM leases WHERE id=?", (lid,)).fetchone()
        assert lease["closed_at"] is not None and lease["close_reason"] == "revoked"
        implicit = e.h.conn.execute(
            "SELECT closed_at FROM leases WHERE job_id=? AND kind='implicit'", (jid,)
        ).fetchall()
        assert all(r["closed_at"] is not None for r in implicit)
    finally:
        await e.aclose()


async def test_reload_rotates_admin_key(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    e = ApiEnv(tmp_path, config_path=cfg_path)
    new_admin = "lh_rotated_admin_key_0000"
    try:
        raw = e.cfg.model_dump(mode="json")
        raw["api"]["admin_key_sha256"] = hash_key(new_admin)
        cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        assert (await e.post("/admin/reload_clients", key=ADMIN_KEY)).status_code == 200
        assert (await e.get("/admin/status", key=ADMIN_KEY)).status_code == 401
        assert (await e.get("/admin/status", key=new_admin)).status_code == 200
    finally:
        await e.aclose()


async def test_reload_busy_keeps_old_clients_admin_and_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    e = ApiEnv(tmp_path, config_path=cfg_path)
    try:
        jid = (await e.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
        raw = e.cfg.model_dump(mode="json")
        raw["clients"]["game"]["cloud_allowed"] = False
        raw["api"]["admin_key_sha256"] = hash_key("lh_other_admin_key_000000")
        cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        old_admin = e.cfg.api.admin_key_sha256

        def busy(*_a: object, **_k: object) -> None:
            raise Busy("database is locked")

        monkeypatch.setattr(auth.queue, "cancel", busy)
        with pytest.raises(Busy):
            e.registry.reload(cfg_path)
        assert e.cfg.clients["game"].cloud_allowed is True
        assert e.cfg.api.admin_key_sha256 == old_admin
        p = e.registry.by_key(GAME_KEY)
        assert p is not None and p.cloud_allowed
        row = e.h.conn.execute("SELECT data_class FROM clients WHERE id='game'").fetchone()
        assert row["data_class"] == "cloud_allowed"
        assert e.job(jid)["status"] == "queued"
    finally:
        await e.aclose()


# --- stream X-LLMHub-Fallback ------------------------------------------------------------


async def test_stream_carries_fallback_header(tmp_path: Path) -> None:
    env = await IntEnv(tmp_path).start()
    try:
        reservations.reserve(env.conn, "session", "external", 45.0, env.clock.now())
        env.rt.cloud.invalidate()  # type: ignore[attr-defined]
        r = await env.post(
            "/v1/chat/completions", chat_body("g", model="big", stream=True), pump_s=30
        )
        assert r.status_code == 200
        assert r.headers["X-LLMHub-Fallback"] == "budget_limit_reached"
        assert "data: [DONE]" in r.text
    finally:
        await env.stop()
