"""T-369 W2: keys, registry, reload, SIGHUP, admin key."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml

from api_fixtures import ADMIN_KEY, GAME_KEY, PRIV_KEY, ApiEnv, api_config, chat_body
from llmhub.api_gateway.auth import ClientRegistry, generate_key, hash_key, install_sighup
from llmhub.configuration.loader import ConfigError


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


def test_hash_and_generate() -> None:
    k = generate_key()
    assert k.startswith("lh_") and len(k) > 40
    assert hash_key("a") == hash_key("a") != hash_key("b")
    assert len(hash_key("x")) == 64
    assert generate_key() != generate_key()


async def test_sync_upsert_and_disable(tmp_path: Path) -> None:
    e = ApiEnv(tmp_path)
    rows = {r["id"]: r for r in e.h.conn.execute("SELECT * FROM clients")}
    assert set(rows) == {"game", "priv", "low"}
    assert rows["game"]["data_class"] == "cloud_allowed"
    assert rows["priv"]["data_class"] == "private"
    assert rows["game"]["key_hash"] == hash_key(GAME_KEY)
    del e.cfg.clients["low"]
    e.registry.sync()
    low = e.h.conn.execute("SELECT disabled_at FROM clients WHERE id='low'").fetchone()
    assert low["disabled_at"] is not None
    e.registry.sync()  # idempotent
    assert e.registry.by_key(GAME_KEY) is not None
    await e.aclose()


async def test_no_plain_keys_in_db_file(env: ApiEnv, tmp_path: Path) -> None:
    await env.post("/v1/chat/completions", chat_body(), pump_s=3)
    env.h.conn.execute("PRAGMA wal_checkpoint(FULL)")
    blob = b"".join(p.read_bytes() for p in tmp_path.glob("state.db*"))
    for key in (GAME_KEY, PRIV_KEY, ADMIN_KEY):
        assert key.encode() not in blob
    assert hash_key(GAME_KEY).encode() in blob


async def test_client_auth_failures(env: ApiEnv) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"},
                    {"Authorization": "Bearer "}):  # fmt: skip
        r = await env.client.get("/v1/models", headers=headers)
        assert r.status_code == 401, headers
        assert r.json()["error"]["code"] == "unauthorized"
        assert r.headers["WWW-Authenticate"] == "Bearer"
    assert (await env.get("/v1/models")).status_code == 200


async def test_disabled_client_is_401(env: ApiEnv) -> None:
    del env.cfg.clients["game"]
    env.registry.sync()
    assert (await env.get("/v1/models")).status_code == 401


async def test_client_key_on_admin_is_403_admin_key_on_v1_is_401(env: ApiEnv) -> None:
    assert (await env.get("/admin/status", key=GAME_KEY)).status_code == 403
    assert (await env.get("/admin/status", key="nonsense")).status_code == 401
    assert (await env.get("/admin/status", key=None)).status_code == 401
    assert (await env.get("/v1/models", key=ADMIN_KEY)).status_code == 401
    assert (await env.get("/admin/status", key=ADMIN_KEY)).status_code == 200


async def test_admin_key_none_gives_403_on_admin_and_metrics(tmp_path: Path) -> None:
    cfg = api_config(tmp_path, api={})
    e = ApiEnv(tmp_path, cfg)
    try:
        for path in ("/admin/status", "/admin/budget", "/metrics"):
            for key in (ADMIN_KEY, GAME_KEY, None):
                r = await e.get(path, key=key)
                assert r.status_code == 403, (path, key)
    finally:
        await e.aclose()


def _write_cfg(path: Path, tmp: Path, clients: dict[str, dict[str, object]]) -> None:
    raw = {
        "data_dir": str(tmp),
        "pools": {"p1": {"provider": "mock", "gpu": "G", "vram_gb": 24, "pp_tps": 1, "tg_tps": 1,
                         "image": "i", "disk_gb": 1, "models": ["big"]}},
        "local_backends": {"cpu": {"url": "http://llama.invalid/v1", "model": "m",
                                   "ctx_tokens": 4096, "pp_tps": 1, "tg_tps": 1}},
        "models": {"big": {"local": "cpu"}},
        "clients": clients,
    }  # fmt: skip
    path.write_text(yaml.safe_dump(raw))


def _client(key: str) -> dict[str, object]:
    return {"key_sha256": hash_key(key), "allowed_models": ["*"], "cloud_allowed": False}


async def test_reload_new_key_works_removed_client_401_broken_keeps_old(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    _write_cfg(path, tmp_path, {"game": _client(GAME_KEY)})
    e = ApiEnv(tmp_path, config_path=path)
    try:
        assert (await e.get("/v1/models")).status_code == 200
        assert (await e.get("/v1/models", key="lh_new_key_xxxxxxxxxxxxxxxx")).status_code == 401
        _write_cfg(path, tmp_path, {"other": _client("lh_new_key_xxxxxxxxxxxxxxxx")})
        r = await e.post("/admin/reload_clients", key=ADMIN_KEY)
        assert r.status_code == 200 and r.json() == {"clients": 1}
        assert (await e.get("/v1/models", key="lh_new_key_xxxxxxxxxxxxxxxx")).status_code == 200
        assert (await e.get("/v1/models")).status_code == 401
        path.write_text("clients: [broken")
        bad = await e.post("/admin/reload_clients", key=ADMIN_KEY)
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "invalid_request"
        assert (await e.get("/v1/models", key="lh_new_key_xxxxxxxxxxxxxxxx")).status_code == 200
        with pytest.raises(ConfigError):
            e.registry.reload(path)
    finally:
        await e.aclose()


async def test_reload_without_config_path_is_400(env: ApiEnv) -> None:
    r = await env.post("/admin/reload_clients", key=ADMIN_KEY)
    assert r.status_code == 400


async def test_sighup_calls_reload(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    _write_cfg(path, tmp_path, {"game": _client(GAME_KEY)})
    e = ApiEnv(tmp_path, config_path=path)
    try:
        install_sighup(asyncio.get_running_loop(), e.registry, path)
        _write_cfg(path, tmp_path, {"x": _client("lh_hup_key_xxxxxxxxxxxxxxxxx")})
        os.kill(os.getpid(), signal.SIGHUP)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if e.registry.by_key("lh_hup_key_xxxxxxxxxxxxxxxxx") is not None:
                break
        assert e.registry.by_key("lh_hup_key_xxxxxxxxxxxxxxxxx") is not None
    finally:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGHUP)
        await e.aclose()


def test_registry_type() -> None:
    assert ClientRegistry.by_key.__name__ == "by_key"
