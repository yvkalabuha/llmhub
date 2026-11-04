"""T-369 review cycle 1 (security & privacy): auth surface, IDOR, data_class, secrets, leakage.

Tests marked `xfail(strict=True)` document an OPEN finding of the review (they flip to XPASS,
i.e. fail, once the finding is fixed: then drop the marker).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from api_fixtures import ADMIN_KEY, GAME_KEY, PRIV_KEY, ApiEnv, api_config, chat_body
from llmhub.api_gateway.logging import SecretMaskFilter
from llmhub.configuration.api_models import ApiConfig
from llmhub.model_router.secrets import detect_secrets

SK = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ApiEnv]:
    e = ApiEnv(tmp_path)
    yield e
    await e.aclose()


def _routes(env: ApiEnv) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for r in env.app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods or ()):
                out.append((m, r.path.replace("{task_id}", "x").replace("{lease_id}", "x")))
    if not out:  # FastAPI 0.162 lazy include_router: fall back to the module routers
        from llmhub.api_gateway import (
            routes_admin,
            routes_health,
            routes_leases,
            routes_openai,
            routes_tasks,
        )

        for mod in (routes_openai, routes_tasks, routes_leases, routes_admin, routes_health):
            for r in mod.router.routes:
                if isinstance(r, APIRoute):
                    for m in sorted(r.methods or ()):
                        p = r.path.replace("{task_id}", "x").replace("{lease_id}", "x")
                        out.append((m, p))
    return out


def _cloud_jobs(env: ApiEnv) -> list[Any]:
    return env.jobs("pool IS NOT NULL")


# --- auth surface ------------------------------------------------------------------------


async def test_every_route_is_protected_except_health(env: ApiEnv) -> None:
    routes = _routes(env)
    assert len(routes) >= 20
    public = {"/healthz", "/readyz"}
    for method, path in routes:
        r = await env.call(method, path, key=None)
        if path in public:
            assert r.status_code in (200, 503), path
            continue
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.headers.get("WWW-Authenticate") == "Bearer"
        # blank / malformed bearer is the same as missing
        for hdr in ("Bearer ", "Bearer    ", "Basic abc", GAME_KEY, "bearer"):
            r = await env.call(method, path, key=None, headers={"Authorization": hdr})
            assert r.status_code == 401, (method, path, hdr)


async def test_admin_and_metrics_need_admin_key_enumerated(env: ApiEnv) -> None:
    admin = [(m, p) for m, p in _routes(env) if p.startswith("/admin") or p == "/metrics"]
    assert {p for _, p in admin} >= {
        "/admin/kill", "/admin/resume", "/admin/clear_weights_hold", "/admin/reload_clients",
        "/admin/hold", "/admin/release", "/admin/reload_billing", "/metrics",
    }  # fmt: skip
    for method, path in admin:
        assert (await env.call(method, path, key=GAME_KEY)).status_code == 403, path
        assert (await env.call(method, path, key="lh_wrong_key_123456")).status_code == 401
    assert (await env.call("GET", "/metrics", key=ADMIN_KEY)).status_code == 200


async def test_admin_none_disables_admin_even_with_any_key(tmp_path: Path) -> None:
    cfg = api_config(tmp_path, api={"admin_key_sha256": None})
    e = ApiEnv(tmp_path, cfg)
    try:
        for method, path in _routes(e):
            if not (path.startswith("/admin") or path == "/metrics"):
                continue
            for key in (None, ADMIN_KEY, GAME_KEY, ""):
                r = await e.call(method, path, key=key)
                assert r.status_code == 403, (path, key)
    finally:
        await e.aclose()


async def test_docs_and_openapi_json_are_public(env: ApiEnv) -> None:
    """SEC-L2 (accepted for a VPN-only service): schema/docs pages need no key."""
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert (await env.get(path, key=None)).status_code == 200, path


def test_bind_default_is_loopback() -> None:
    assert ApiConfig().host == "127.0.0.1"


async def test_no_cors_headers(env: ApiEnv) -> None:
    r = await env.get("/v1/models", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert not any(k.lower().startswith("access-control-") for k in r.headers)
    pre = await env.call(
        "OPTIONS", "/v1/chat/completions", key=None,
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )  # fmt: skip
    assert pre.status_code in (401, 404, 405)
    assert "access-control-allow-origin" not in {k.lower() for k in pre.headers}


async def test_openapi_has_no_secret_material(env: ApiEnv) -> None:
    spec = env.app.openapi()
    text = json.dumps(spec)
    admin = env.cfg.api.admin_key_sha256
    assert admin is not None and admin not in text
    for c in env.cfg.clients.values():
        assert c.key_sha256 not in text
    repo = Path(__file__).resolve().parents[1]
    on_disk = (repo / "docs" / "openapi.yaml").read_text(encoding="utf-8")
    import re

    assert not re.search(r"lh_[A-Za-z0-9_-]{16,}|[0-9a-f]{64}", on_disk)


def test_examples_embed_no_keys() -> None:
    import re

    repo = Path(__file__).resolve().parents[1]
    bad = re.compile(r"lh_[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{20,}")
    for p in (repo / "examples").rglob("*"):
        if p.is_file() and p.suffix in {".sh", ".py", ".cs", ".yaml", ".md", ".csproj"}:
            assert not bad.search(p.read_text(encoding="utf-8")), p


# --- client isolation (IDOR) -------------------------------------------------------------


async def test_foreign_task_status_result_cancel_are_404(env: ApiEnv) -> None:
    env.worker.hold = True
    jid = (await env.post("/v1/tasks", chat_body(model="localonly"))).json()["id"]
    for method, url in (("GET", f"/v1/tasks/{jid}"), ("GET", f"/v1/tasks/{jid}/result"),
                        ("POST", f"/v1/tasks/{jid}/cancel")):  # fmt: skip
        r = await env.call(method, url, key=PRIV_KEY)
        assert r.status_code == 404, url
        assert jid not in r.text
    row = env.job(jid)
    assert row["status"] == "queued" and not row["cancel_requested"]
    assert env.worker.cancelled == []


async def test_idempotency_key_is_scoped_per_client(env: ApiEnv) -> None:
    env.worker.hold = True
    h = {"Idempotency-Key": "shared-key-1"}
    a = (await env.post("/v1/tasks", chat_body("one", model="localonly"), headers=h)).json()
    b = await env.post("/v1/tasks", chat_body("one", model="localonly"), headers=h, key=PRIV_KEY)
    assert b.status_code == 202 and b.json()["id"] != a["id"]
    c = await env.post("/v1/tasks", chat_body("two", model="localonly"), headers=h, key=PRIV_KEY)
    # priv's own key conflict: the task id in the 409 is priv's, never game's
    assert c.status_code == 409 and a["id"] not in c.text


async def test_implicit_dedup_is_scoped_per_client(env: ApiEnv) -> None:
    env.worker.hold = True
    a = (await env.post("/v1/tasks", chat_body("same", model="localonly"))).json()["id"]
    b = (await env.post("/v1/tasks", chat_body("same", model="localonly"), key=PRIV_KEY)).json()
    assert b["id"] != a


async def test_foreign_lease_header_renew_close_are_404(env: ApiEnv) -> None:
    lid = (await env.post("/v1/leases", {"model": "big", "ttl_s": 300})).json()["id"]
    r = await env.post(
        "/v1/tasks", chat_body(model="localonly"), key=PRIV_KEY, headers={"X-LLMHub-Lease": lid}
    )
    assert r.status_code == 404
    for url in (f"/v1/leases/{lid}/renew", f"/v1/leases/{lid}/close"):
        assert (await env.post(url, {}, key=PRIV_KEY)).status_code == 404
    row = env.h.conn.execute("SELECT closed_at FROM leases WHERE id=?", (lid,)).fetchone()
    assert row["closed_at"] is None


# --- data_class: private never reaches the cloud -----------------------------------------


async def test_private_narrowing_never_reattaches_a_cloud_job(env: ApiEnv) -> None:
    h = {"Idempotency-Key": "k-cloud-1"}
    first = await env.post("/v1/tasks", chat_body("x", model="cloudonly"), headers=h)
    assert first.status_code == 202
    cloud_id = first.json()["id"]
    assert env.job(cloud_id)["pool"] == "p1"
    for extra in ({"X-LLMHub-Data-Class": "private"}, {}):
        hh = {**h, **extra} if extra else h
        body = chat_body("x", model="cloudonly")
        if not extra:
            body["llmhub"] = {"data_class": "private"}
        r = await env.post("/v1/tasks", body, headers=hh)
        assert r.status_code in (403, 409), r.text
        assert r.status_code != 200
    # same without an Idempotency-Key: implicit dedup must not re-attach either
    r = await env.post(
        "/v1/tasks", chat_body("x", model="cloudonly"), headers={"X-LLMHub-Data-Class": "private"}
    )
    assert r.status_code == 403
    r = await env.post(
        "/v1/tasks", chat_body("x", model="big"), headers={"X-LLMHub-Data-Class": "private"}
    )
    assert r.status_code == 202 and env.job(r.json()["id"])["pool"] is None
    assert [j["id"] for j in _cloud_jobs(env)] == [cloud_id]


async def test_private_client_cannot_widen(env: ApiEnv) -> None:
    for hdr, body_dc in (("cloud_allowed", None), (None, "cloud_allowed"),
                         ("cloud_allowed", "cloud_allowed")):  # fmt: skip
        body = chat_body("w", model="cloudonly")
        if body_dc:
            body["llmhub"] = {"data_class": body_dc}
        headers = {"X-LLMHub-Data-Class": hdr} if hdr else {}
        r = await env.post("/v1/tasks", body, key=PRIV_KEY, headers=headers)
        assert r.status_code == 403
    r = await env.post("/v1/chat/completions", chat_body("w", model="cloudonly", stream=True),
                       key=PRIV_KEY)  # fmt: skip
    assert r.status_code == 403
    assert _cloud_jobs(env) == []


async def test_duplicate_data_class_header_any_private_wins(env: ApiEnv) -> None:
    body = json.dumps(chat_body("d", model="cloudonly")).encode()
    headers = [
        ("Authorization", f"Bearer {GAME_KEY}"), ("Content-Type", "application/json"),
        ("X-LLMHub-Data-Class", "private"), ("X-LLMHub-Data-Class", "cloud_allowed"),
    ]  # fmt: skip
    r = await env.client.post("/v1/tasks", content=body, headers=httpx.Headers(headers))
    assert r.status_code == 403
    assert _cloud_jobs(env) == []


# --- secret detector via the API ---------------------------------------------------------


def _with(where: str) -> dict[str, Any]:
    b = chat_body("hi", model="cloudonly")
    if where == "content_parts":
        b["messages"] = [{"role": "user", "content": [{"type": "text", "text": f"k={SK}"}]}]
    elif where == "tool_args":
        b["messages"].append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "f", "arguments": json.dumps({"token": SK})},
                    }
                ],
            }
        )
    elif where == "tool_result":
        b["messages"].append({"role": "tool", "tool_call_id": "c1", "content": SK})
    elif where == "tools":
        b["tools"] = [{"type": "function", "function": {"name": "f", "description": SK}}]
    elif where == "response_format":
        b["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "x", "schema": {"default": SK}},
        }
    elif where == "unknown_field":
        b["metadata"] = {"note": SK}
    elif where == "name":
        b["messages"][0]["name"] = SK
    return b


@pytest.mark.parametrize(
    "where",
    ["content_parts", "tool_args", "tool_result", "tools", "response_format", "unknown_field",
     "name"],
)  # fmt: skip
async def test_secret_anywhere_never_goes_to_cloud(env: ApiEnv, where: str) -> None:
    r = await env.post("/v1/tasks", _with(where))
    assert r.status_code == 403, where
    assert SK not in r.text
    assert _cloud_jobs(env) == []


async def test_secret_written_with_json_unicode_escapes_is_detected(env: ApiEnv) -> None:
    raw = (
        '{"model":"cloudonly","messages":[{"role":"user","content":"'
        + "".join(f"\\u{ord(c):04x}" for c in SK)
        + '"}]}'
    )
    r = await env.call("POST", "/v1/tasks", content=raw.encode(),
                       headers={"Content-Type": "application/json"})  # fmt: skip
    assert r.status_code == 403
    assert _cloud_jobs(env) == []


async def test_secret_with_stream_and_chat_path(env: ApiEnv) -> None:
    for stream in (False, True):
        r = await env.post("/v1/chat/completions", chat_body(f"use {SK}", model="cloudonly",
                                                             stream=stream))  # fmt: skip
        assert r.status_code == 403
    assert _cloud_jobs(env) == []


async def test_deeply_nested_body_fails_closed(env: ApiEnv) -> None:
    deep: Any = SK
    for _ in range(3000):
        deep = [deep]
    b = chat_body("x", model="cloudonly")
    b["metadata"] = deep
    raw = json.dumps(b)
    r = await env.call("POST", "/v1/tasks", content=raw.encode(),
                       headers={"Content-Type": "application/json"})  # fmt: skip
    assert r.status_code >= 400
    assert SK not in r.text
    assert _cloud_jobs(env) == []


async def test_error_bodies_never_echo_input(env: ApiEnv) -> None:
    bad = ('{"model": "cloudonly", "messages": "' + SK + '"').encode()
    r = await env.call("POST", "/v1/tasks", content=bad,
                       headers={"Content-Type": "application/json"})  # fmt: skip
    assert r.status_code == 400 and SK not in r.text
    r = await env.post("/v1/tasks", {"model": SK, "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code >= 400 and SK not in r.text
    r = await env.post("/v1/tasks", chat_body("x", model="cloudonly"),
                       headers={"X-LLMHub-Task-Type": SK})  # fmt: skip
    assert r.status_code == 400 and SK not in r.text


# Known detector gaps (documented limitations / findings SEC-M2).
@pytest.mark.parametrize(
    "req",
    [
        pytest.param({"messages": [{"role": "user", "content": "x"}], "metadata": {SK: 1}},
                     id="dict_key_with_non_string_value"),
        pytest.param({"messages": [{"content": "sk_live_" + "a" * 24}]}, id="stripe_sk_live"),
        pytest.param({"messages": [{"content": "ghs_" + "a" * 36}]}, id="github_ghs"),
        pytest.param({"messages": [{"content": "glpat-" + "a" * 20}]}, id="gitlab_glpat"),
    ],
)  # fmt: skip
def test_detector_gaps(req: dict[str, Any]) -> None:
    assert detect_secrets(req)


@pytest.mark.parametrize(
    "req",
    [
        pytest.param({"messages": [{"content": "c2stQTFiMkMzZDRFNWY2RzdoOEk5ajBLMWwy"}]},
                     id="base64"),
        pytest.param({"messages": [{"content": "my key is sk-"},
                                   {"content": "A1b2C3d4E5f6G7h8I9j0K1l2"}]}, id="split"),
        pytest.param({"messages": [{"content": "s\u200bk-A1b2C3d4E5f6G7h8I9j0K1l2"}]},
                     id="zero_width"),
    ],
)  # fmt: skip
def test_detector_accepted_limitations_are_not_detected(req: dict[str, Any]) -> None:
    """Heuristic detector: obfuscated secrets pass (documented; `private` is the real guard)."""
    assert detect_secrets(req) == []


# --- body size / DoS ---------------------------------------------------------------------


async def test_chunked_body_over_limit_is_413(env: ApiEnv) -> None:
    limit = env.cfg.api.max_body_bytes

    async def gen() -> AsyncIterator[bytes]:
        chunk = b" " * 65536
        sent = 0
        while sent <= limit:
            yield chunk
            sent += len(chunk)

    r = await env.call("POST", "/v1/tasks", content=gen(),
                       headers={"Content-Type": "application/json"})  # fmt: skip
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "request_too_large"


async def test_declared_length_over_limit_is_413_even_unauthenticated(env: ApiEnv) -> None:
    n = env.cfg.api.max_body_bytes + 1
    r = await env.call("POST", "/v1/tasks", key=None, content=b"x" * n)
    assert r.status_code == 413


async def test_unauthenticated_body_is_rejected_before_parsing(env: ApiEnv) -> None:
    r = await env.call("POST", "/v1/chat/completions", key=None, content=b"{not json")
    assert r.status_code == 401


# --- log leakage -------------------------------------------------------------------------


def _filtered(msg: str, *args: object, exc: BaseException | None = None) -> str:
    """Filter + format one record directly (independent of global logging state)."""
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, msg, args, exc_info)
    assert SecretMaskFilter().filter(record)
    text = logging.Formatter().format(record)
    assert text
    return text


def test_mask_filter_masks_message() -> None:
    text = _filtered("auth %s", f"Authorization: Bearer {GAME_KEY}")
    assert GAME_KEY not in text and "auth" in text


def test_mask_filter_masks_traceback_text() -> None:
    try:
        raise RuntimeError(f"upstream said: Authorization: Bearer {GAME_KEY}")
    except RuntimeError as exc:
        text = _filtered("Exception in ASGI application", exc=exc)
    assert "Traceback" in text
    assert GAME_KEY not in text


# --- revocation via reload ---------------------------------------------------------------


def _write_cfg(path: Path, env: ApiEnv, game_cloud: bool) -> None:
    import yaml

    raw = env.cfg.model_dump(mode="json")
    raw["clients"]["game"]["cloud_allowed"] = game_cloud
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")


async def test_revoking_cloud_allowed_stops_queued_cloud_jobs(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    e = ApiEnv(tmp_path, config_path=cfg_path)
    try:
        jid = (await e.post("/v1/tasks", chat_body("q", model="cloudonly"))).json()["id"]
        assert e.job(jid)["pool"] == "p1"
        _write_cfg(cfg_path, e, game_cloud=False)
        assert (await e.post("/admin/reload_clients", key=ADMIN_KEY)).status_code == 200
        assert e.job(jid)["status"] in ("cancelled", "failed")
    finally:
        await e.aclose()


async def test_revoking_cloud_allowed_blocks_lease_renew(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    e = ApiEnv(tmp_path, config_path=cfg_path)
    try:
        lid = (await e.post("/v1/leases", {"model": "big", "ttl_s": 300})).json()["id"]
        _write_cfg(cfg_path, e, game_cloud=False)
        assert (await e.post("/admin/reload_clients", key=ADMIN_KEY)).status_code == 200
        r = await e.post(f"/v1/leases/{lid}/renew", {})
        assert r.status_code == 403
    finally:
        await e.aclose()


async def test_reload_disables_removed_client_immediately(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    e = ApiEnv(tmp_path, config_path=cfg_path)
    try:
        import yaml

        raw = e.cfg.model_dump(mode="json")
        del raw["clients"]["priv"]
        cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        assert (await e.post("/admin/reload_clients", key=ADMIN_KEY)).status_code == 200
        assert (await e.get("/v1/models", key=PRIV_KEY)).status_code == 401
        # a broken file keeps the old set and the error text carries no config values
        cfg_path.write_text("clients: {game: {key_sha256: 'zz', allowed_models: []}}\n")
        r = await e.post("/admin/reload_clients", key=ADMIN_KEY)
        assert r.status_code == 400 and "zz" not in r.json()["error"]["message"]
        assert (await e.get("/v1/models")).status_code == 200
    finally:
        await e.aclose()


async def test_reload_rejects_client_hash_equal_to_running_admin_hash(tmp_path: Path) -> None:
    import yaml

    from llmhub.api_gateway.auth import hash_key

    cfg_path = tmp_path / "cfg.yaml"
    e = ApiEnv(tmp_path, config_path=cfg_path)
    try:
        raw = e.cfg.model_dump(mode="json")
        raw["api"]["admin_key_sha256"] = hash_key("lh_rotated_admin_key_0000")
        raw["clients"]["game"]["key_sha256"] = hash_key(ADMIN_KEY)  # operator mistake
        cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        r = await e.post("/admin/reload_clients", key=ADMIN_KEY)
        assert r.status_code == 400
    finally:
        await e.aclose()
